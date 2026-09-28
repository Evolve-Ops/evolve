"""Tests for the plugin capability-widening gate.

The gate exists because ``deploy.install_oc_plugin`` passes
``--accept-capabilities`` (OC 2026.9.2 makes the consent prompt terminal and
deploy is headless), which blanket-answers OC's own widening check. Spec
[internal/spec-plugin-install-trust-2026-06-06.md](../../internal/spec-plugin-install-trust-2026-06-06.md)
§4.2 / §4.3.

The load-bearing assertion in here is
``test_widening_in_source_passes_the_digests_but_fails_the_gate``: it is the
whole reason this gate is separate from the digests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
if str(_ADMIN_DIR) not in sys.path:
    sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin import plugin_signature  # noqa: E402
from evolve_admin.plugin_capability_baseline import BASELINE  # noqa: E402
from evolve_admin.plugin_capability_surface import (  # noqa: E402
    HOOK_GRANTS_GROUP, SURFACE_GROUPS, declared_surface, diff_narrowing,
    diff_widening,
)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_MANIFEST = _REPO_ROOT / "packages" / "plugin" / "openclaw.plugin.json"


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text())


# ── Extraction ──────────────────────────────────────────────────────────────

def test_every_group_present_even_when_empty():
    """A comparison never has to tell 'absent' from 'empty'."""
    surface = declared_surface({})
    assert set(surface) == set(SURFACE_GROUPS) | {HOOK_GRANTS_GROUP}
    assert all(v == [] for v in surface.values())


def test_surface_groups_mirror_openclaw_verbatim():
    """Pinned against OC 2026.9.2's PLUGIN_DECLARED_SURFACE_GROUPS.

    Order and spelling both matter — this is a mirror of someone else's
    definition, and a silent rename upstream is how the two drift apart.
    """
    assert SURFACE_GROUPS == (
        "channels", "providers", "tools", "contracts", "hooks", "mcpServers",
        "cliCommands", "cliBackends", "skills", "dangerousConfigFlags",
    )


def test_contracts_are_family_qualified():
    """OC renders contracts as '<family>: <id>', so two families declaring the
    same id stay distinguishable."""
    surface = declared_surface(
        {"contracts": {"tools": ["x"], "webSearchProviders": ["x"]}}
    )
    assert surface["contracts"] == ["tools: x", "webSearchProviders: x"]
    assert surface["tools"] == ["x"]


def test_unreviewed_contract_family_contributes_nothing():
    """Mirrors upstream: a family outside REVIEWED_MANIFEST_CONTRACT_FAMILIES
    is not part of the surface. Documented, not endorsed."""
    assert declared_surface({"contracts": {"somethingNew": ["x"]}})["contracts"] == []


def test_hook_grants_captured_from_the_object_form():
    """Evolve's manifest uses hooks-as-object; OC reads hooks-as-array for
    declared names and files these under `grants`. We track them anyway."""
    surface = declared_surface(
        {"hooks": {"allowConversationAccess": True, "allowPromptInjection": False}}
    )
    assert surface["hooks"] == []
    assert surface[HOOK_GRANTS_GROUP] == ["allowConversationAccess"]


def test_hooks_array_form_reads_as_declared_names():
    surface = declared_surface({"hooks": ["before_tool_call"]})
    assert surface["hooks"] == ["before_tool_call"]
    assert surface[HOOK_GRANTS_GROUP] == []


def test_malformed_manifest_does_not_raise():
    """This runs on the install path; the digest check owns fail-closed."""
    for junk in ({"contracts": "nope"}, {"hooks": 3}, {"providers": [1, None]},
                 {"channels": "x"}, {"toolMetadata": []}):
        assert declared_surface(junk)


def test_tool_metadata_keys_count_as_tools():
    surface = declared_surface(
        {"contracts": {"tools": ["a"]}, "toolMetadata": {"b": {}}}
    )
    assert surface["tools"] == ["a", "b"]


# ── Diffing ─────────────────────────────────────────────────────────────────

def test_widening_and_narrowing_are_separate_verdicts():
    base = {"tools": ["a", "b"]}
    cur = {"tools": ["b", "c"]}
    assert diff_widening(base, cur) == {"tools": ["c"]}
    assert diff_narrowing(base, cur) == {"tools": ["a"]}


def test_group_missing_from_baseline_is_reported_whole():
    """OC added a group, we mirrored it, the baseline predates it — those
    entries are genuinely unreviewed."""
    assert diff_widening({}, {"skills": ["s"]}) == {"skills": ["s"]}


def test_narrowing_alone_is_not_widening():
    assert diff_widening({"tools": ["a", "b"]}, {"tools": ["a"]}) == {}


# ── The baseline tracks the real manifest ───────────────────────────────────

def test_committed_baseline_matches_the_committed_manifest():
    """The gate is only meaningful while the baseline describes reality.
    Re-freeze with `tools/plugin-capability-lint --update-baseline`."""
    assert diff_widening(BASELINE, declared_surface(_manifest())) == {}
    assert diff_narrowing(BASELINE, declared_surface(_manifest())) == {}


def test_baseline_still_pins_the_widest_privileges():
    """A regeneration that quietly dropped these would be the failure this
    whole gate exists to prevent, and it would still look 'clean'."""
    assert "allowConversationAccess" in BASELINE[HOOK_GRANTS_GROUP]
    assert "agentToolResultMiddleware: openclaw" in BASELINE["contracts"]
    assert "gmail_send" in BASELINE["tools"]


# ── Wiring into the trust gate ──────────────────────────────────────────────

def test_clean_manifest_passes_the_gate():
    assert plugin_signature.check_capability_baseline(_manifest()) == (True, "")


def test_widening_in_source_passes_the_digests_but_fails_the_gate():
    """THE point of this gate.

    A capability added to our own manifest and built normally produces a
    perfectly valid stamp — treeDigest hashes the manifest's content, so it
    only catches a capability added by tampering with an already-built tree.
    Nothing else on the install path would notice this.
    """
    m = _manifest()
    m["contracts"]["tools"].append("drive_delete_everything")
    ok, msg = plugin_signature.check_capability_baseline(m)
    assert not ok
    assert "drive_delete_everything" in msg
    assert "--update-baseline" in msg  # names the remedy


def test_narrowing_does_not_fail_the_gate():
    m = _manifest()
    m["contracts"]["tools"] = m["contracts"]["tools"][:3]
    assert plugin_signature.check_capability_baseline(m)[0]


def test_new_hook_grant_is_caught():
    m = _manifest()
    m["hooks"]["allowPromptInjection"] = True
    ok, msg = plugin_signature.check_capability_baseline(m)
    assert not ok and "allowPromptInjection" in msg


def test_ships_permissive():
    """Staged rollout: widening warns, it does not refuse the install.
    Flipping this is gated on `release rollback` forcing a plugin rebuild —
    read REQUIRE_CAPABILITY_BASELINE before changing this."""
    assert plugin_signature.REQUIRE_CAPABILITY_BASELINE is False


def _stamped_plugin_dir(tmp_path: Path, manifest: dict) -> Path:
    d = tmp_path / "evolve-plugin"
    (d / "dist").mkdir(parents=True)
    (d / "dist" / "index.js").write_text("console.log(1)\n")
    (d / "node_modules").mkdir()
    (d / "package.json").write_text("{}\n")
    (d / "package-lock.json").write_text("{}\n")
    (d / "openclaw.plugin.json").write_text(json.dumps(manifest))
    plugin_signature.stamp_install_tree(d)
    return d


def test_widened_surface_warns_but_installs_while_staged(tmp_path):
    m = _manifest()
    m["contracts"]["tools"].append("drive_delete_everything")
    ok, msg = plugin_signature.verify_plugin_signature(_stamped_plugin_dir(tmp_path, m))
    assert ok, "must not refuse the install while REQUIRE_CAPABILITY_BASELINE is False"
    assert "drive_delete_everything" in msg


def test_widened_surface_refuses_once_required(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_signature, "REQUIRE_CAPABILITY_BASELINE", True)
    m = _manifest()
    m["contracts"]["tools"].append("drive_delete_everything")
    ok, msg = plugin_signature.verify_plugin_signature(_stamped_plugin_dir(tmp_path, m))
    assert not ok and "drive_delete_everything" in msg


def test_clean_manifest_still_verifies_with_no_warning(tmp_path):
    """The gate must not make every ordinary install noisy."""
    assert plugin_signature.verify_plugin_signature(
        _stamped_plugin_dir(tmp_path, _manifest())
    ) == (True, "")


def test_capability_warning_survives_a_legacy_stamp(tmp_path, monkeypatch):
    """A legacy-stamped pod is precisely one that has not rebuilt in a while —
    the case where an unreviewed surface is most likely sitting unnoticed. The
    legacy early-return must not swallow the capability warning."""
    m = _manifest()
    m["contracts"]["tools"].append("drive_delete_everything")
    d = _stamped_plugin_dir(tmp_path, m)
    manifest_path = d / "openclaw.plugin.json"
    data = json.loads(manifest_path.read_text())
    data["x-evolve-trust"].pop("treeDigest")          # simulate a pre-treeDigest stamp
    data["x-evolve-trust"].pop("treeDigestAlgorithm")
    manifest_path.write_text(json.dumps(data))

    ok, msg = plugin_signature.verify_plugin_signature(d)
    assert ok
    assert "treeDigest" in msg, "legacy warning lost"
    assert "drive_delete_everything" in msg, "capability warning lost"


# ── The CI gate ─────────────────────────────────────────────────────────────

def _lint(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "tools" / "plugin-capability-lint"), *args],
        capture_output=True, text=True,
    )


def test_lint_passes_on_the_committed_tree():
    r = _lint()
    assert r.returncode == 0, r.stdout + r.stderr


def test_lint_show_emits_the_surface():
    r = _lint("--show")
    assert r.returncode == 0
    assert json.loads(r.stdout) == declared_surface(_manifest())


def test_lint_fails_when_the_manifest_widens(tmp_path):
    """Drive the REAL script — the gate has to fail the PR, not just the
    library. Against a temp manifest, never the committed one: the admin suite
    runs four sharded processes over a single checkout."""
    m = _manifest()
    m["contracts"]["tools"].append("drive_delete_everything")
    widened = tmp_path / "openclaw.plugin.json"
    widened.write_text(json.dumps(m))

    r = _lint("--manifest", str(widened))
    assert r.returncode == 1
    assert "drive_delete_everything" in r.stderr
    assert "--update-baseline" in r.stderr


def test_lint_reports_narrowing_without_failing(tmp_path):
    m = _manifest()
    dropped = m["contracts"]["tools"].pop()
    narrowed = tmp_path / "openclaw.plugin.json"
    narrowed.write_text(json.dumps(m))

    r = _lint("--manifest", str(narrowed))
    assert r.returncode == 0, "narrowing is safe — it must not fail the build"
    assert dropped in r.stdout and "--update-baseline" in r.stdout


def test_update_baseline_round_trips(tmp_path):
    """Regenerating from a widened manifest makes the gate pass again — and the
    generated module is importable Python, not just text."""
    m = _manifest()
    m["contracts"]["tools"].append("drive_delete_everything")
    manifest = tmp_path / "openclaw.plugin.json"
    manifest.write_text(json.dumps(m))
    baseline = tmp_path / "plugin_capability_baseline.py"

    assert _lint("--manifest", str(manifest), "--baseline", str(baseline),
                 "--update-baseline").returncode == 0
    ns: dict = {}
    exec(compile(baseline.read_text(), str(baseline), "exec"), ns)
    assert "drive_delete_everything" in ns["BASELINE"]["tools"]
    assert _lint("--manifest", str(manifest), "--baseline", str(baseline)).returncode == 0
