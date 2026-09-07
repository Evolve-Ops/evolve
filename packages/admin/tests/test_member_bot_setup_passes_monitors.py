"""tests/test_member_bot_setup_passes_monitors.py — fresh-bot acceptance gate.

Setup wizard previously produced bots that failed five monitor checks on the
first audit pass — missing MEMORY.md/README.md, stub SOUL.md, missing 7
openclaw.json baseline fields with the wrong tools.exec.security, and a
missing brave plugin entry. This test pins the shape the wizard now writes
so any regression in `_setup_oc_for_bot`, the bot-workspace templates, or
`install_member_bot_docs` fails CI rather than getting caught by an operator's
Alerts page on the next live setup.

Three guarantees, each tested:
  1. The starter openclaw.json `_setup_oc_for_bot` writes satisfies every
     permission_monitor baseline field (no `perm_config_drift`).
  2. The shipped bot-workspace templates render to files that satisfy the
     content_scan structural_emptiness pattern (SOUL.md and AGENTS.md both
     ≥1500 bytes after `{bot_id}`/`{role}` substitution) and the
     content_scan_file_disappeared check (all four files present).
  3. plugin_monitor's v2 baseline ships with an empty required_plugins
     list, so a fresh bot can't fail the required-plugin check on its
     first audit pass. See internal/spec-plugin-posture-rework-2026-06-06.md.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from evolve_admin.deploy import (  # noqa: E402
    _MEMBER_BOT_DOCS_TEMPLATE_DIR,
    _MEMBER_BOT_DOC_FILES,
    _apply_permission_baseline_gap_fills,
    _render_member_bot_doc,
)
from permissions import baseline as _perm_baseline  # noqa: E402
from permissions import inventory as _perm_inv  # noqa: E402
from plugins import bootstrap as _plug_bootstrap  # noqa: E402
from content_scan.default_patterns import default_catalog  # noqa: E402
from content_scan import patterns as _scan_patterns  # noqa: E402


# ── 1. Permission baseline ────────────────────────────────────────────────────


def _starter_openclaw_json(home: Path, port: int = 19030) -> dict:
    """Mirror of the openclaw.json template `_setup_oc_for_bot` writes.

    Kept in sync with packages/admin/evolve_admin/setup_wizard.py
    `_setup_oc_for_bot`. If that template changes shape, update this
    function and the test exercises the new shape automatically.
    """
    return {
        "agents": {
            "defaults": {
                "workspace": str(home / ".openclaw" / "workspace"),
            }
        },
        "gateway": {"port": port},
        "plugins": {"entries": {}},
        "tools": {
            # Member-bot default since 2026-05-25 pivot —
            # internal/spec-app-derived-permissions-2026-05-24.md. Tracks
            # setup_wizard._setup_oc_for_bot's actual write shape.
            "exec": {"security": "full", "ask": "on-miss"},
            "web": {
                "search": {"enabled": True},
                "fetch": {"enabled": True},
            },
        },
        "commands": {
            "native": "auto",
            "nativeSkills": "auto",
        },
        # Tracks setup_wizard._setup_oc_for_bot's logging block, which
        # points OC's logger at a bounded on-disk file so the gateway
        # plist's StandardOut/Err capture doesn't grow unbounded.
        "logging": {
            "file": str(home / ".openclaw" / "logs" / "openclaw.log"),
            "maxFileBytes": 26214400,
        },
        # No top-level "sandbox" — OC's config schema rejects it (the valid
        # path is agents.defaults.sandbox.mode). Test mirrors setup_wizard's
        # current write shape.
    }


def test_starter_openclaw_json_has_no_permission_drift():
    """The shape `_setup_oc_for_bot` writes must satisfy every baseline field.

    Runs the permission inventory reader against a synthetic openclaw.json and
    diffs the parsed fields against DEFAULT_BASELINE.pod_default. Zero diffs
    means a fresh bot will produce zero `perm_config_drift` signals on first
    monitor pass.
    """
    with tempfile.TemporaryDirectory() as tmp:
        oc_path = Path(tmp) / "openclaw.json"
        oc_path.write_text(json.dumps(_starter_openclaw_json(Path(tmp))))

        pc = _perm_inv._read_permission_config(oc_path)
        assert pc.read_error is None, f"unexpected read error: {pc.read_error}"

        expected = _perm_baseline.resolve(_perm_baseline.DEFAULT_BASELINE, "any_bot")
        diffs = {
            fld: {"expected": expected[fld], "observed": pc.fields.get(fld)}
            for fld in expected
            if pc.fields.get(fld) != expected[fld]
        }
        assert not diffs, f"permission drift vs baseline: {diffs}"


# Root-level keys OC's config schema recognizes. Empirically verified against
# OC 2026.4.29 on the test pod via ``openclaw config validate --json`` — both
# the published docs at docs.openclaw.ai/gateway/configuration-reference and a
# direct kitchen-sink validation run; "multiAgent" appears in the docs but the
# validator rejects it, "commands" / "meta" / "memory" appear in working bot
# configs and validate cleanly. The OC validator rejects any other root key,
# which makes ``safe_write_bot_config`` abort the deploy. If OC adds a new
# root key, add it here.
_OC_VALID_ROOT_KEYS = frozenset({
    "channels", "agents", "session", "messages", "memory", "talk", "tools",
    "models", "mcp", "skills", "plugins", "commitments", "browser", "ui",
    "gateway", "hooks", "discovery", "env", "secrets", "auth", "logging",
    "diagnostics", "update", "acp", "cli", "wizard", "cron", "commands",
    "meta",
})


def test_permission_baseline_uses_only_valid_oc_root_keys():
    """Regression guard against the #1260 / #1271 deploy-blocking bug class.

    Before #1271, the permission baseline expected a ``sandbox.enabled`` field
    that didn't exist in OC's config schema. ``ensure_plugin_config`` wrote
    that as a root-level ``sandbox`` key, OC's validator rejected it as
    "Unrecognized key", and ``safe_write_bot_config`` aborted every deploy.
    #1271 removed the field; this test pins three facts that together make
    the regression impossible to reintroduce:

      1. The baseline only names fields whose root segment is a real OC root key.
      2. The deploy-time gap-fill only adds keys at OC-valid root paths.
      3. The inventory reader's PERMISSION_CONFIG_FIELDS shares (1)'s shape.

    The earlier tests round-tripped through Python dicts and never validated
    against the OC schema, which is why they passed while every live deploy
    failed. This test bridges that gap by asserting the contract at the dotpath
    level without needing the openclaw binary.

    If OC adds a new root key (e.g. ``policies``), update _OC_VALID_ROOT_KEYS.
    """
    # 1. Baseline keys.
    pod_default = _perm_baseline.DEFAULT_BASELINE["pod_default"]["permission_config"]
    for dotpath in pod_default:
        root = dotpath.split(".", 1)[0]
        assert root in _OC_VALID_ROOT_KEYS, (
            f"DEFAULT_BASELINE field {dotpath!r} has root {root!r} which is not "
            f"in OC's config schema. ensure_plugin_config will write it at the "
            f"root of openclaw.json and OC's validator will reject the deploy. "
            f"Either drop the field or move it under a valid root."
        )

    # 2. Gap-fill output. Start from the minimal config _setup_oc_for_bot writes
    # and apply the gap-fill in isolation. Every root key produced must be valid.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _starter_openclaw_json(Path(tmp))
        _apply_permission_baseline_gap_fills(cfg)
        bad_roots = set(cfg.keys()) - _OC_VALID_ROOT_KEYS
        assert not bad_roots, (
            f"_apply_permission_baseline_gap_fills produced root keys not in OC's "
            f"schema: {bad_roots}. OC's validator will reject the deploy."
        )

    # 3. Inventory field tuple stays in lockstep with (1).
    for dotpath in _perm_inv.PERMISSION_CONFIG_FIELDS:
        root = dotpath.split(".", 1)[0]
        assert root in _OC_VALID_ROOT_KEYS, (
            f"PERMISSION_CONFIG_FIELDS contains {dotpath!r} with root {root!r}, "
            f"which OC doesn't recognize. Drop it or move under a valid root."
        )


# ── 2. Content-scan templates ────────────────────────────────────────────────


def test_member_bot_doc_templates_are_present():
    """All four templates must ship — missing template = silent regression to
    the 'Evolve creates broken OC bots' state."""
    missing = [
        f for f in _MEMBER_BOT_DOC_FILES
        if not (_MEMBER_BOT_DOCS_TEMPLATE_DIR / f).exists()
    ]
    assert not missing, f"missing templates: {missing}"


def test_soul_and_agents_render_above_structural_floor():
    """Structural_emptiness fires alerts below 1500 bytes (the April-2026 team_bot_a
    AGENTS.md truncation pattern). SOUL.md and AGENTS.md must both clear
    that threshold after `{bot_id}` + `{role}` substitution.
    """
    floor = 1500  # mirrors min_size_bytes in default_patterns.structural_emptiness
    for fname in ("SOUL.md", "AGENTS.md"):
        rendered = _render_member_bot_doc(
            _MEMBER_BOT_DOCS_TEMPLATE_DIR / fname,
            bot_id="testbot", role="member",
        )
        size = len(rendered.encode("utf-8"))
        assert size >= floor, f"{fname} renders to {size} bytes (<{floor})"


def test_rendered_templates_pass_structural_pattern():
    """Run the actual content_scan structural pattern against rendered
    templates — belt-and-suspenders against any future change to how
    structural_emptiness is computed (e.g. min_size_bytes bumped to 2000)."""
    cat = default_catalog()
    for fname in ("SOUL.md", "AGENTS.md"):
        text = _render_member_bot_doc(
            _MEMBER_BOT_DOCS_TEMPLATE_DIR / fname,
            bot_id="testbot", role="member",
        )
        matches = _scan_patterns.scan_file(
            text=text,
            filename=fname,
            patterns=cat.deny_patterns,
            evolve_markers_allowlist=cat.evolve_markers_allowlist,
            file_size_bytes=len(text.encode("utf-8")),
        )
        structural = [m for m in matches if m.to_dict().get("pattern_kind") == "structural"]
        assert not structural, (
            f"{fname} fires structural pattern: "
            f"{[m.to_dict() for m in structural]}"
        )


def test_placeholders_are_substituted():
    """Catch a future template that uses a placeholder name the renderer
    doesn't substitute — would leave `{bot_id}` literal in the deployed file."""
    rendered = _render_member_bot_doc(
        _MEMBER_BOT_DOCS_TEMPLATE_DIR / "SOUL.md",
        bot_id="personal_bot", role="member",
    )
    assert "{bot_id}" not in rendered
    assert "{role}" not in rendered
    assert "personal_bot" in rendered


# ── 3. Plugin baseline ────────────────────────────────────────────────────────


def test_v2_baseline_required_plugins_is_empty():
    """The v2 plugin baseline ships with an empty required_plugins list
    so a fresh bot can't fail plugin_missing_required on its first audit
    pass. ``evolve`` is intentionally not required (its absence shows up
    as heartbeat loss), and ``brave`` is one option among several
    web-search backends. See
    internal/spec-plugin-posture-rework-2026-06-06.md §2.6.
    """
    baseline = _plug_bootstrap.build_default_baseline()
    assert baseline.required_plugins == [], (
        f"v2 default required_plugins must be empty, got {baseline.required_plugins!r}"
    )
