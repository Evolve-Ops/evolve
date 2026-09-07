"""tests/test_auth_profile_normalization.py — auth-profile key normalization.

OpenClaw's runtime profile lookup is keyed on ``<provider>:<id>``
(colon-separated), but OC's ``onboard --anthropic-api-key X`` flag
writes the profile under the key ``anthropic_api_key``
(underscore-separated). The result: model.primary is correctly set
to anthropic/something, but the runtime can't find an anthropic
credential because it's looking for ``anthropic:*`` keys.

Live-observed on atlas 2026-05-28:
  - Atlas's openclaw.json model.primary = anthropic/claude-sonnet-4-6
  - Atlas's auth-profiles.json key = "anthropic_api_key" (underscore)
  - Forge dispatch → OC fails to find anthropic key
  - FailoverError → walks bundled fallback → tries openai → no key
  - Step 2 "Build" fails with the misleading "no api key for openai"

Coverage:
  - Any profile with underscore shape and a provider field gets renamed
    to ``<provider>:<type>`` (matches the verify gauntlet's expectation
    and the rotate endpoint's rename-on-write behaviour)
  - Already-canonical keys are no-op
  - Multiple renames in one pass
  - Target-key collision (canonical version already exists) is preserved
  - Permission errors / missing file return ok=True with explanatory reason
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.provisioning import (  # noqa: E402
    _auth_profile_keys_need_normalization,
    normalize_auth_profile_keys,
)


# ── _auth_profile_keys_need_normalization (pure) ────────────────────────────


def test_renames_anthropic_underscore_to_colon():
    """The atlas case: anthropic_api_key → anthropic:api_key."""
    profiles = {
        "anthropic_api_key": {
            "provider": "anthropic",
            "type": "api_key",
            "key": "sk-ant-...",
        },
    }
    renames = _auth_profile_keys_need_normalization(profiles)
    assert renames == {"anthropic_api_key": "anthropic:api_key"}


def test_leaves_canonical_colon_keys_alone():
    """Team_bot_a's shape — already in colon form, no renames needed."""
    profiles = {
        "anthropic:api": {"provider": "anthropic", "type": "api_key", "key": "x"},
        "openai:default": {"provider": "openai", "type": "api_key", "key": "y"},
    }
    assert _auth_profile_keys_need_normalization(profiles) == {}


def test_renames_brave_underscore_to_colon():
    """Brave keys live in auth-profiles.json the same way LLM keys do;
    the wizard-verify gauntlet flags ``brave_api_key`` as legacy, so the
    batch normalizer must rename it too. Surfaced on evo 2026-06-01
    where the verify error survived a Brave key rotation because the
    rotate path didn't normalize the key shape."""
    profiles = {
        "brave_api_key": {"provider": "brave", "type": "api_key", "key": "x"},
    }
    renames = _auth_profile_keys_need_normalization(profiles)
    assert renames == {"brave_api_key": "brave:api_key"}


def test_renames_only_verify_flagged_legacy_keys():
    """Scope = wizard-verify's `_LEGACY_KEY_RE` exactly:
    `^(anthropic|openai|brave)_(api_key|auth_token)$`. Keys outside
    that set are left alone — e.g. token_pair entries like
    `telegram_token_pair` legitimately use underscores."""
    profiles = {
        "anthropic_api_key": {"provider": "anthropic", "type": "api_key", "key": "a"},
        "anthropic_auth_token": {"provider": "anthropic", "type": "auth_token", "key": "b"},
        "openai_api_key": {"provider": "openai", "type": "api_key", "key": "c"},
        "brave_api_key": {"provider": "brave", "type": "api_key", "key": "d"},
        # Below: legitimately not flagged by verify; leave them alone.
        "telegram_token_pair": {
            "provider": "telegram", "type": "token_pair",
            "bot_token": "x", "chat_id": "y",
        },
        "runway_default": {"provider": "runway", "type": "api_key", "key": "e"},
        "openai_default": {"provider": "openai", "type": "api_key", "key": "f"},
    }
    renames = _auth_profile_keys_need_normalization(profiles)
    assert renames == {
        "anthropic_api_key": "anthropic:api_key",
        "anthropic_auth_token": "anthropic:auth_token",
        "openai_api_key": "openai:api_key",
        "brave_api_key": "brave:api_key",
    }


def test_skips_legacy_shape_when_provider_field_missing():
    """If a key matches the legacy regex but the entry has no
    ``provider`` field, we can't derive a canonical name confidently —
    skip rather than guess from the key prefix. Operator-resolvable."""
    profiles = {
        "anthropic_api_key": {"type": "api_key", "key": "x"},
    }
    renames = _auth_profile_keys_need_normalization(profiles)
    assert renames == {}


def test_empty_profiles_returns_empty():
    assert _auth_profile_keys_need_normalization({}) == {}


# ── normalize_auth_profile_keys (writes via /tmp + sudo /bin/cp) ────────────


def _make_atlas_profile_file(dir: Path) -> Path:
    """Build a temp file matching atlas's actual on-disk shape."""
    p = dir / "auth-profiles.json"
    p.write_text(json.dumps({
        "profiles": {
            "brave_api_key": {
                "provider": "brave", "type": "api_key", "key": "brave-key",
            },
            "anthropic_api_key": {
                "provider": "anthropic", "type": "api_key", "key": "sk-ant-x",
            },
        }
    }, indent=2))
    return p


def test_normalize_writes_renamed_profiles(tmp_path):
    """End-to-end: atlas-shape file → normalize → verify on-disk content
    has anthropic:api_key and no anthropic_api_key."""
    ap_dir = tmp_path / ".openclaw/agents/main/agent"
    ap_dir.mkdir(parents=True)
    profile_file = ap_dir / "auth-profiles.json"
    profile_file.write_text(_make_atlas_profile_file(tmp_path).read_text())

    # Patch the module's _user_home binding (post-platform_profile sweep the
    # path is built inside evolve_config, so Path() interception can't see it)
    # AND patch subprocess.run so the sudo writes are mocked but recorded.
    captured_writes: list[dict] = []

    def fake_run(cmd, **kwargs):
        # First sudo cp copies our /tmp staging → dest; capture the dest
        # path so we can verify the right file was targeted.
        from subprocess import CompletedProcess
        if cmd[0] == "sudo" and cmd[1] == "/bin/cp":
            # Read the staged content + write it to where the cp would
            # have landed so the assertion below can read it.
            src, dst = cmd[2], cmd[3]
            Path(dst).write_text(Path(src).read_text())
            captured_writes.append({"cp": (src, dst)})
            return CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "sudo" and cmd[1] in ("/usr/sbin/chown", "/bin/chmod"):
            return CompletedProcess(cmd, 0, "", "")
        return CompletedProcess(cmd, 0, "", "")

    with patch("evolve_admin.provisioning._user_home", lambda u: tmp_path), \
         patch("evolve_admin.provisioning.subprocess.run", side_effect=fake_run):
        result = normalize_auth_profile_keys("atlas")

    assert result["ok"] is True
    rename_keys = {r["from"]: r["to"] for r in result["renames"]}
    # Both LLM and non-LLM providers get normalized — the canonical
    # name is derived from each entry's own provider+type fields.
    assert rename_keys == {
        "anthropic_api_key": "anthropic:api_key",
        "brave_api_key": "brave:api_key",
    }

    # Confirm the file on disk now has the renamed keys
    written = json.loads(profile_file.read_text())
    profile_keys = set(written["profiles"].keys())
    assert "anthropic:api_key" in profile_keys
    assert "brave:api_key" in profile_keys
    assert "anthropic_api_key" not in profile_keys
    assert "brave_api_key" not in profile_keys


def test_normalize_idempotent_when_all_canonical(tmp_path):
    """Re-running on an already-canonical file is a no-op."""
    profile_file = tmp_path / "auth-profiles.json"
    profile_file.write_text(json.dumps({
        "profiles": {
            "anthropic:api_key": {"provider": "anthropic", "type": "api_key", "key": "x"},
            "brave:api_key": {"provider": "brave", "type": "api_key", "key": "y"},
        }
    }))

    with patch("evolve_admin.provisioning.Path") as MockPath, \
         patch("evolve_admin.provisioning.subprocess.run") as mock_run:
        MockPath.side_effect = lambda x: profile_file if "auth-profiles" in str(x) else Path(x)
        result = normalize_auth_profile_keys("team_bot_a")

    assert result["ok"] is True
    assert result["renames"] == []
    # No sudo cp call when nothing to rename
    assert not any(
        c.args[0] == "sudo" and c.args[1] == "/bin/cp"
        for c in mock_run.call_args_list
        if c.args and len(c.args[0]) >= 2
    )


def test_normalize_preserves_existing_target_on_collision(tmp_path):
    """If BOTH anthropic_api_key AND anthropic:api_key exist, keep the
    canonical one and don't overwrite. Surface the skip in result."""
    ap_dir = tmp_path / ".openclaw/agents/main/agent"
    ap_dir.mkdir(parents=True)
    profile_file = ap_dir / "auth-profiles.json"
    profile_file.write_text(json.dumps({
        "profiles": {
            "anthropic_api_key": {"provider": "anthropic", "type": "api_key", "key": "OLD"},
            "anthropic:api_key": {"provider": "anthropic", "type": "api_key", "key": "NEW"},
        }
    }))

    captured_writes: list = []

    def fake_run(cmd, **kwargs):
        from subprocess import CompletedProcess
        if cmd[0] == "sudo" and cmd[1] == "/bin/cp":
            src, dst = cmd[2], cmd[3]
            Path(dst).write_text(Path(src).read_text())
            captured_writes.append(("cp", src, dst))
            return CompletedProcess(cmd, 0, "", "")
        return CompletedProcess(cmd, 0, "", "")

    with patch("evolve_admin.provisioning._user_home", lambda u: tmp_path), \
         patch("evolve_admin.provisioning.subprocess.run", side_effect=fake_run):
        result = normalize_auth_profile_keys("team_bot_a")

    # Skipped collision is reported; canonical NEW value preserved
    assert result["ok"] is True
    assert any(s["to"] == "anthropic:api_key" for s in result.get("skipped", []))
    written = json.loads(profile_file.read_text())
    assert written["profiles"]["anthropic:api_key"]["key"] == "NEW"


def test_normalize_returns_ok_when_no_file(tmp_path):
    """Bot with no auth-profiles.json yet → no-op, ok=True."""
    nonexistent = tmp_path / "nonexistent.json"
    with patch("evolve_admin.provisioning.Path") as MockPath:
        MockPath.side_effect = lambda x: nonexistent if "auth-profiles" in str(x) else Path(x)
        result = normalize_auth_profile_keys("newbot")
    assert result["ok"] is True
    assert result["renames"] == []
    assert "no auth-profiles.json yet" in result["reason"]
