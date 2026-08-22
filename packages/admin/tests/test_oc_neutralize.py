"""Tests for evolve_admin.oc_neutralize — the `--neutralize-externalized`
upgrade dance helpers.

Covers the pure logic (strip_externalized_refs, extract_install_error)
plus the snapshot/neutralize/restore flow against a tmp-rooted bot home
with subprocess.run mocked. Network/filesystem-touching helpers
(install_externalized_plugin) we exercise via their wrapped command —
no integration test because the install requires a real openclaw runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import oc_neutralize as ocn


# ── strip_externalized_refs ──────────────────────────────────────────────────

def test_strip_deletes_plugins_entries_for_named_ids():
    cfg = {
        "plugins": {"entries": {
            "brave":     {"enabled": True},
            "slack":     {"enabled": True},
            "anthropic": {"enabled": True},
        }},
    }
    out = ocn.strip_externalized_refs(cfg, {"brave", "slack"})
    assert set(out["plugins"]["entries"].keys()) == {"anthropic"}


def test_strip_disables_named_channels_without_dropping_their_config():
    """Channels carry tokens/secrets — preserve the config block so the
    operator's setup isn't lost, just flip the enable flag to false."""
    cfg = {
        "channels": {
            "slack":    {"enabled": True, "botToken": "xoxb-secret",
                         "channels": {"C123": {"requireMention": True}}},
            "telegram": {"enabled": True},
        },
    }
    out = ocn.strip_externalized_refs(cfg, {"slack"})
    assert out["channels"]["slack"]["enabled"] is False
    # Token + per-channel settings preserved
    assert out["channels"]["slack"]["botToken"] == "xoxb-secret"
    assert out["channels"]["slack"]["channels"] == {"C123": {"requireMention": True}}
    # Untouched channel left alone
    assert out["channels"]["telegram"]["enabled"] is True


def test_strip_clears_web_search_provider_when_it_names_a_missing_plugin():
    cfg = {"tools": {"web": {"search": {"provider": "brave"}}}}
    out = ocn.strip_externalized_refs(cfg, {"brave"})
    assert "provider" not in out["tools"]["web"]["search"]


def test_strip_leaves_web_search_provider_when_unrelated():
    cfg = {"tools": {"web": {"search": {"provider": "duckduckgo"}}}}
    out = ocn.strip_externalized_refs(cfg, {"brave"})
    assert out["tools"]["web"]["search"]["provider"] == "duckduckgo"


def test_strip_is_pure_does_not_mutate_input():
    cfg = {"plugins": {"entries": {"brave": {"enabled": True}}}}
    original = json.loads(json.dumps(cfg))
    _ = ocn.strip_externalized_refs(cfg, {"brave"})
    assert cfg == original, "strip_externalized_refs mutated input"


def test_strip_tolerates_missing_blocks():
    """Bots with no plugins.entries / no channels / no tools.web.search
    section shouldn't crash."""
    assert ocn.strip_externalized_refs({}, {"brave"}) == {}
    assert ocn.strip_externalized_refs(
        {"plugins": {}}, {"brave"},
    ) == {"plugins": {}}


# ── extract_install_error ────────────────────────────────────────────────────

def test_extract_install_error_prefers_npm_error_code_over_rerun_tail():
    """The classic case: real EACCES message buried above npm's generic
    'rerun with --loglevel=verbose' tail."""
    stderr = """
Resolving clawhub:@openclaw/brave-plugin…
npm view failed: npm error code EACCES
npm error syscall mkdir
npm error path /Users/pod_admin_user/.npm/_cacache/tmp
npm error errno EACCES
npm error You can rerun the command with `--loglevel=verbose` to see the logs in your terminal
""".strip()
    got = ocn.extract_install_error("", stderr)
    assert "EACCES" in got
    assert "rerun the command" not in got


def test_extract_install_error_catches_config_invalid_message():
    stderr = """
OpenClaw config is invalid
  - tools.web.search.provider: web_search provider is not available: brave
Fix: openclaw doctor --fix
npm error You can rerun the command with `--loglevel=verbose` to see the logs in your terminal
""".strip()
    got = ocn.extract_install_error("", stderr)
    assert "OpenClaw config is invalid" in got


def test_extract_install_error_catches_plugin_api_mismatch():
    stderr = 'Plugin "@openclaw/brave-plugin" requires plugin API >=2026.5.12, but this OpenClaw runtime exposes 2026.4.29.'
    got = ocn.extract_install_error("", stderr)
    assert "requires plugin API" in got


def test_extract_install_error_labels_unmatched_output_as_unmatched():
    """No known pattern matches → still show the last line (it's a clue), but
    label it, because an unlabelled quote reads as the diagnosis."""
    got = ocn.extract_install_error("first line\nsomething useful here", "")
    assert "something useful here" in got
    assert "no recognized error line" in got


def test_extract_install_error_does_not_report_a_progress_line_as_the_cause():
    """The 2026-08-17 pod case: `openclaw plugins install` failed with an
    empty stderr and a chatty stdout, and the operator was handed the
    install's own progress line as the error — which reads like success.

        re-pin failed: codex (…): Installing @openclaw/codex@2026.7.1-1 into …
    """
    stdout = (
        "Installing @openclaw/codex@2026.7.1-1 into "
        "/Users/team-bot-a/.openclaw/npm/projects/openclaw-codex-8902d781d4…"
    )
    got = ocn.extract_install_error(stdout, "")
    assert got.startswith("no recognized error line"), (
        "a progress line must never be presented as the cause"
    )


def test_extract_install_error_handles_empty():
    assert ocn.extract_install_error("", "") == "unknown error"


# ── snapshot_and_neutralize_bot — integration with mocked subprocess ─────────

@pytest.fixture
def fake_bot_home(tmp_path, monkeypatch):
    """Mock the read + write helpers against an in-memory bot config so we
    can exercise snapshot_and_neutralize_bot's logic without /Users/<user>/
    paths or root sudo. Returns the captured-writes dict so each test can
    assert what landed where.
    """
    captured: dict[str, str] = {}
    initial = json.dumps({
        "plugins": {"entries": {"brave": {"enabled": True},
                                "anthropic": {"enabled": True}}},
        "channels": {"slack": {"enabled": True, "botToken": "xoxb-xyz"}},
        "tools": {"web": {"search": {"provider": "brave"}}},
    }, indent=2)

    monkeypatch.setattr(ocn, "_read_bot_openclaw_json_text",
                        lambda user: initial)

    def fake_write(content, dest, owner):
        captured[str(dest)] = content
        return True, ""

    monkeypatch.setattr(ocn, "_write_bot_file", fake_write)
    return captured


def test_snapshot_and_neutralize_writes_backup_and_neutralized_live(fake_bot_home):
    result = ocn.snapshot_and_neutralize_bot(
        bot_id="team_bot_a", user="team_bot_a", plugin_ids={"brave", "slack"},
    )
    assert result.ok, result.error
    assert result.plugin_ids == ["brave", "slack"]

    # Backup gets the verbatim pre-neutralize content
    backup_text = fake_bot_home["/Users/team_bot_a/.openclaw/openclaw.json.preupgrade"]
    backup_cfg = json.loads(backup_text)
    assert "brave" in backup_cfg["plugins"]["entries"]
    assert backup_cfg["channels"]["slack"]["enabled"] is True
    assert backup_cfg["tools"]["web"]["search"]["provider"] == "brave"

    # Live file is neutralized: brave entry gone, slack channel disabled (but
    # botToken preserved), web-search provider key dropped
    live_text = fake_bot_home["/Users/team_bot_a/.openclaw/openclaw.json"]
    live_cfg = json.loads(live_text)
    assert "brave" not in live_cfg["plugins"]["entries"]
    assert live_cfg["channels"]["slack"]["enabled"] is False
    assert live_cfg["channels"]["slack"]["botToken"] == "xoxb-xyz"
    assert "provider" not in live_cfg["tools"]["web"]["search"]


def test_snapshot_and_neutralize_reports_parse_error_clearly(monkeypatch):
    monkeypatch.setattr(ocn, "_read_bot_openclaw_json_text",
                        lambda user: "{not valid json")
    monkeypatch.setattr(ocn, "_write_bot_file",
                        lambda *a, **kw: (True, ""))

    result = ocn.snapshot_and_neutralize_bot(
        bot_id="team_bot_a", user="team_bot_a", plugin_ids={"brave"},
    )
    assert result.ok is False
    assert "parse error" in result.error


def test_snapshot_and_neutralize_reports_unreadable_clearly(monkeypatch):
    monkeypatch.setattr(ocn, "_read_bot_openclaw_json_text",
                        lambda user: None)
    monkeypatch.setattr(ocn, "_write_bot_file",
                        lambda *a, **kw: (True, ""))

    result = ocn.snapshot_and_neutralize_bot(
        bot_id="team_bot_a", user="team_bot_a", plugin_ids={"brave"},
    )
    assert result.ok is False
    assert "could not read" in result.error


# ── _resolve_install_spec — auto-pinning the install spec ───────────────────

class TestResolveInstallSpec:
    """OC 2026.5.18 added the `plugins.installs_unpinned_npm_specs` audit
    finding (severity warn). Every install record's `spec` field must be
    `<name>@X.Y.Z`. The auto-pin logic in _resolve_install_spec routes
    @openclaw/* installs through `<pkg>@<oc_version>`; everything else
    falls back to explicit caller-supplied versions or passes through.
    """

    def test_explicit_version_appended(self, monkeypatch):
        monkeypatch.setattr(ocn, "_installed_openclaw_version", lambda: None)
        assert ocn._resolve_install_spec(
            "@openclaw/brave-plugin", "2026.5.18",
        ) == "@openclaw/brave-plugin@2026.5.18"

    def test_explicit_version_wins_over_auto_pin(self, monkeypatch):
        """Caller-supplied version takes precedence over the auto-pin
        derived from the runtime — useful for testing against a specific
        upgrade target during the neutralize-externalized dance."""
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.20")
        assert ocn._resolve_install_spec(
            "@openclaw/brave-plugin", "2026.5.18",
        ) == "@openclaw/brave-plugin@2026.5.18"

    def test_openclaw_package_auto_pins_to_runtime_version(self, monkeypatch):
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.18")
        assert ocn._resolve_install_spec(
            "@openclaw/brave-plugin", None,
        ) == "@openclaw/brave-plugin@2026.5.18"

    def test_openclaw_package_passes_through_when_runtime_unreadable(
            self, monkeypatch,
    ):
        """If we can't read /opt/homebrew/lib/node_modules/openclaw/
        package.json, fall back to the bare spec rather than passing
        `pkg@None` to npm. The audit will still flag it, but at least
        install proceeds."""
        monkeypatch.setattr(ocn, "_installed_openclaw_version", lambda: None)
        assert ocn._resolve_install_spec(
            "@openclaw/brave-plugin", None,
        ) == "@openclaw/brave-plugin"

    def test_already_pinned_scope_pkg_passes_through(self, monkeypatch):
        """Caller already encoded `@scope/name@version` — don't append a
        second `@<oc_version>` (would produce
        `@openclaw/brave-plugin@2026.5.18@2026.5.19`)."""
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.20")
        assert ocn._resolve_install_spec(
            "@openclaw/brave-plugin@2026.5.18", None,
        ) == "@openclaw/brave-plugin@2026.5.18"

    def test_already_tagged_passes_through(self, monkeypatch):
        """Operator explicitly asked for a dist-tag — pass through. The
        audit will still re-flag it (only `X.Y.Z` shapes satisfy OC's
        isPinnedRegistrySpec regex), but that's the operator's call to
        override."""
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.20")
        assert ocn._resolve_install_spec(
            "@openclaw/brave-plugin@latest", None,
        ) == "@openclaw/brave-plugin@latest"

    def test_unscoped_package_with_version_passes_through(self, monkeypatch):
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.20")
        assert ocn._resolve_install_spec("brave@1.2.3", None) == "brave@1.2.3"

    def test_third_party_scoped_package_not_auto_pinned(self, monkeypatch):
        """Auto-pin to OC version only makes sense for @openclaw/* —
        third-party plugins have their own release cadence. Pass
        through; the operator can pass `version=` explicitly when
        they want pinning."""
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.18")
        assert ocn._resolve_install_spec(
            "@some-vendor/their-plugin", None,
        ) == "@some-vendor/their-plugin"

    def test_unscoped_package_not_auto_pinned(self, monkeypatch):
        monkeypatch.setattr(ocn, "_installed_openclaw_version",
                            lambda: "2026.5.18")
        assert ocn._resolve_install_spec("some-plugin", None) == "some-plugin"


# ── install_externalized_plugin — end-to-end with mocked subprocess ─────────

def test_install_externalized_plugin_pins_openclaw_packages(monkeypatch):
    """install_externalized_plugin('@openclaw/brave-plugin') should result
    in `openclaw plugins install --force @openclaw/brave-plugin@<oc_ver>`
    being run — not the bare name (which is what triggers OC's
    plugins.installs_unpinned_npm_specs audit finding)."""
    monkeypatch.setattr(ocn, "_installed_openclaw_version",
                        lambda: "2026.5.18")

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocn.subprocess, "run", fake_run)

    ok, err = ocn.install_externalized_plugin("team_bot_a", "@openclaw/brave-plugin")
    assert ok and err == ""
    assert captured["cmd"] == [
        "sudo", "-u", "team_bot_a", "-H", "openclaw", "plugins", "install",
        "--force", "@openclaw/brave-plugin@2026.5.18",
    ]


def test_install_externalized_plugin_honors_explicit_version(monkeypatch):
    monkeypatch.setattr(ocn, "_installed_openclaw_version",
                        lambda: "2026.5.18")

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocn.subprocess, "run", fake_run)

    ok, _ = ocn.install_externalized_plugin(
        "team_bot_a", "@openclaw/brave-plugin", version="2026.5.20",
    )
    assert ok
    assert "@openclaw/brave-plugin@2026.5.20" in captured["cmd"]
    assert "@openclaw/brave-plugin@2026.5.18" not in captured["cmd"]


def test_install_externalized_plugin_skips_pinning_when_runtime_missing(
        monkeypatch,
):
    """If /opt/homebrew/lib/node_modules/openclaw/package.json is
    unreadable, fall back to the bare spec rather than passing
    `pkg@None` to npm. Audit will still flag it; better than failing
    the install outright."""
    monkeypatch.setattr(ocn, "_installed_openclaw_version", lambda: None)

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocn.subprocess, "run", fake_run)

    ok, _ = ocn.install_externalized_plugin("team_bot_a", "@openclaw/brave-plugin")
    assert ok
    assert captured["cmd"][-1] == "@openclaw/brave-plugin"


def test_install_externalized_plugin_force_flag_toggle(monkeypatch):
    monkeypatch.setattr(ocn, "_installed_openclaw_version",
                        lambda: "2026.5.18")

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocn.subprocess, "run", fake_run)

    ok, _ = ocn.install_externalized_plugin(
        "team_bot_a", "@openclaw/brave-plugin", force=False,
    )
    assert ok
    assert "--force" not in captured["cmd"]


def test_snapshot_and_neutralize_propagates_snapshot_write_failure(monkeypatch):
    """If the snapshot can't be written, we must abort BEFORE touching the
    live file — otherwise the operator has no rollback anchor."""
    monkeypatch.setattr(ocn, "_read_bot_openclaw_json_text",
                        lambda user: json.dumps({"plugins": {"entries": {}}}))
    write_calls = []

    def fake_write(content, dest, owner):
        write_calls.append(str(dest))
        # Fail on the snapshot, would succeed on the live file
        if str(dest).endswith(".preupgrade"):
            return False, "disk full"
        return True, ""

    monkeypatch.setattr(ocn, "_write_bot_file", fake_write)

    result = ocn.snapshot_and_neutralize_bot(
        bot_id="team_bot_a", user="team_bot_a", plugin_ids={"brave"},
    )
    assert result.ok is False
    assert "snapshot failed" in result.error
    assert "disk full" in result.error
    # Live file MUST NOT have been touched after snapshot failure
    assert all(p.endswith(".preupgrade") for p in write_calls)


# ── Gap 3: OC runtime version resolves across macOS + Linux install prefixes ──


class TestInstalledOpenclawVersionPlatformAware:
    """`_installed_openclaw_version` must find OC's package.json on a Linux
    pod (NodeSource global prefix /usr/lib/node_modules), not only the macOS
    Homebrew path. A single macOS literal returned None on Linux, so fresh
    @openclaw/* installs went in UNPINNED and the
    `plugins.installs_unpinned_npm_specs` audit fired on bring-up.
    """

    def test_linux_nodesource_path_is_a_candidate(self):
        assert (
            Path("/usr/lib/node_modules/openclaw/package.json")
            in ocn._OPENCLAW_PACKAGE_JSON_CANDIDATES
        )

    def test_resolves_version_from_linux_path_when_macos_absent(
        self, tmp_path, monkeypatch,
    ):
        """Only the Linux candidate exists → version still resolves."""
        linux_pkg = tmp_path / "usr/lib/node_modules/openclaw/package.json"
        linux_pkg.parent.mkdir(parents=True)
        linux_pkg.write_text(json.dumps({"version": "2026.6.20"}))
        macos_pkg = tmp_path / "opt/homebrew/lib/node_modules/openclaw/package.json"
        monkeypatch.setattr(
            ocn, "_OPENCLAW_PACKAGE_JSON_CANDIDATES", (macos_pkg, linux_pkg),
        )
        assert ocn._installed_openclaw_version() == "2026.6.20"

    def test_first_readable_candidate_wins(self, tmp_path, monkeypatch):
        first = tmp_path / "a/package.json"
        first.parent.mkdir(parents=True)
        first.write_text(json.dumps({"version": "1.0.0"}))
        second = tmp_path / "b/package.json"
        second.parent.mkdir(parents=True)
        second.write_text(json.dumps({"version": "2.0.0"}))
        monkeypatch.setattr(
            ocn, "_OPENCLAW_PACKAGE_JSON_CANDIDATES", (first, second),
        )
        assert ocn._installed_openclaw_version() == "1.0.0"

    def test_none_when_no_candidate_readable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ocn, "_OPENCLAW_PACKAGE_JSON_CANDIDATES",
            (tmp_path / "nope/package.json",),
        )
        assert ocn._installed_openclaw_version() is None

    def test_fresh_install_spec_is_pinned_on_linux(self, tmp_path, monkeypatch):
        """End-to-end for the fresh-deploy path: with OC's package.json only
        at the Linux NodeSource path, a fresh @openclaw/* install (version=None)
        auto-pins instead of going in bare."""
        linux_pkg = tmp_path / "usr/lib/node_modules/openclaw/package.json"
        linux_pkg.parent.mkdir(parents=True)
        linux_pkg.write_text(json.dumps({"version": "2026.6.20"}))
        monkeypatch.setattr(
            ocn, "_OPENCLAW_PACKAGE_JSON_CANDIDATES", (linux_pkg,),
        )
        assert ocn._resolve_install_spec("@openclaw/codex", None) == (
            "@openclaw/codex@2026.6.20"
        )
