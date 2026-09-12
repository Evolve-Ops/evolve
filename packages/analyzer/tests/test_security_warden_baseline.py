"""Tests for the redacted-baseline helper.

Goal: a baseline snapshot of openclaw.json can still answer
"did this value change?" without storing the actual secret on disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

from generators.security_warden import baseline  # noqa: E402
from generators.security_warden import scrub_baselines  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# redaction primitives
# ─────────────────────────────────────────────────────────────────────────────


def test_redact_marker_is_stable_for_same_value():
    a = baseline._redact_value("xoxb-1234567890")
    b = baseline._redact_value("xoxb-1234567890")
    assert a == b
    assert a.startswith("<REDACTED:sha256:")
    assert a.endswith(">")


def test_redact_marker_differs_when_value_changes():
    a = baseline._redact_value("xoxb-1234567890")
    b = baseline._redact_value("xoxb-9999999999")
    assert a != b


def test_redact_marker_is_idempotent():
    once = baseline.redact_json({"botToken": "abc-12345-secret-value-here"})
    twice = baseline.redact_json(once)
    assert once == twice


# ─────────────────────────────────────────────────────────────────────────────
# key-name based rules
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_token_is_redacted_at_any_depth():
    src = {
        "channels": {
            "telegram": {"botToken": "1234567890:AAEabcdefghijklmnopqrstuvwxyz0123456"},
            "slack": {"botToken": "xoxb-real-slack-token-value"},
        }
    }
    out = baseline.redact_json(src)
    assert out["channels"]["telegram"]["botToken"].startswith("<REDACTED:sha256:")
    assert out["channels"]["slack"]["botToken"].startswith("<REDACTED:sha256:")
    # The real secret never appears
    serialized = json.dumps(out)
    assert "AAEabcdefghijklmnopqrstuvwxyz0123456" not in serialized
    assert "xoxb-real-slack-token-value" not in serialized


def test_app_token_api_key_secret_password_all_redacted():
    src = {
        "channels": {"slack": {"appToken": "xapp-real-token-value"}},
        "integrations": {
            "anthropic": {"apiKey": "sk-ant-real-key-value"},
            "github": {"clientSecret": "0123456789abcdef"},
        },
        "auth": {"password": "hunter2-with-extra-chars"},
    }
    out = baseline.redact_json(src)
    blob = json.dumps(out)
    for secret in [
        "xapp-real-token-value",
        "sk-ant-real-key-value",
        "0123456789abcdef",
        "hunter2-with-extra-chars",
    ]:
        assert secret not in blob


def test_path_rule_catches_gateway_auth_token():
    src = {"gateway": {"auth": {"token": "a" * 48}}}
    out = baseline.redact_json(src)
    assert out["gateway"]["auth"]["token"].startswith("<REDACTED:sha256:")


def test_discord_token_caught_by_key_name():
    """``channels.discord.token`` has no path rule but the ``token`` key
    matches the secret-key list."""
    src = {"channels": {"discord": {"token": "MTQ3OTUxlive-discord-token-value-here"}}}
    out = baseline.redact_json(src)
    assert out["channels"]["discord"]["token"].startswith("<REDACTED:sha256:")
    assert "MTQ3OTUxlive-discord-token-value-here" not in json.dumps(out)


# ─────────────────────────────────────────────────────────────────────────────
# false-positive guards
# ─────────────────────────────────────────────────────────────────────────────


def test_max_tokens_int_passes_through():
    src = {"agents": {"defaults": {"compaction": {"reserveTokensFloor": 30000}}}}
    out = baseline.redact_json(src)
    assert out["agents"]["defaults"]["compaction"]["reserveTokensFloor"] == 30000


def test_user_token_read_only_bool_passes_through():
    src = {"channels": {"slack": {"userTokenReadOnly": True}}}
    out = baseline.redact_json(src)
    assert out["channels"]["slack"]["userTokenReadOnly"] is True


def test_non_string_secret_keys_pass_through():
    """A boolean at a secret-key path is not a credential — leave it alone."""
    src = {"feature": {"apiKey": None, "secret": False}}
    out = baseline.redact_json(src)
    assert out == src


# ─────────────────────────────────────────────────────────────────────────────
# diff-still-works contract
# ─────────────────────────────────────────────────────────────────────────────


def test_diff_detects_secret_rotation():
    """The redacted marker changes when the underlying secret changes."""
    before = baseline.redact_json({"botToken": "1111:AAEoriginalvaluehere123456789"})
    after = baseline.redact_json({"botToken": "9999:BBBrotatedsecretvaluehere9876"})
    assert before != after
    assert before["botToken"] != after["botToken"]


def test_diff_stable_when_secret_unchanged():
    """Same secret value → same marker. Equality-diff still works."""
    a = baseline.redact_json({"botToken": "1111:AAEoriginalvaluehere123456789"})
    b = baseline.redact_json({"botToken": "1111:AAEoriginalvaluehere123456789"})
    assert a == b


def test_diff_detects_non_secret_field_change():
    """Surrounding fields are not perturbed by redaction — drift on them still shows."""
    src1 = {
        "channels": {"telegram": {"botToken": "1111:secretvalue123456789012345678"}},
        "meta": {"lastTouchedVersion": "2026.5.20"},
    }
    src2 = {
        "channels": {"telegram": {"botToken": "1111:secretvalue123456789012345678"}},
        "meta": {"lastTouchedVersion": "2026.5.21"},
    }
    a = baseline.redact_json(src1)
    b = baseline.redact_json(src2)
    assert a != b
    assert a["channels"] == b["channels"]
    assert a["meta"]["lastTouchedVersion"] != b["meta"]["lastTouchedVersion"]


# ─────────────────────────────────────────────────────────────────────────────
# fallback: leaf-string pattern scanning
# ─────────────────────────────────────────────────────────────────────────────


def test_telegram_token_in_unknown_path_is_caught_by_pattern():
    """Defense in depth: even if the key name doesn't flag, a bare telegram
    token in any string value gets redacted by leaf-pattern fallback."""
    src = {"notes": "field: 1234567890:AAEabcdefghijklmnopqrstuvwxyz0123456"}
    out = baseline.redact_json(src)
    assert "AAEabcdefghijklmnopqrstuvwxyz0123456" not in json.dumps(out)


def test_anthropic_key_in_unknown_path_is_caught_by_pattern():
    src = {"notes": "left a key here: sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"}
    out = baseline.redact_json(src)
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(out)


# ─────────────────────────────────────────────────────────────────────────────
# scrub_baselines CLI / file walker
# ─────────────────────────────────────────────────────────────────────────────


def _write(p: Path, payload: dict | str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        p.write_text(json.dumps(payload, indent=2))
    else:
        p.write_text(payload)


def test_scrub_directory_redacts_json_baseline(tmp_path):
    baseline_file = tmp_path / "team_bot_b-openclaw.json"
    _write(
        baseline_file,
        {
            "channels": {
                "telegram": {
                    "botToken": "1111:AAEoriginalvaluehere1234567890123456"
                }
            },
            "meta": {"lastTouchedVersion": "2026.5.20"},
        },
    )

    counts = scrub_baselines.scrub_directory(tmp_path, dry_run=False)

    after = json.loads(baseline_file.read_text())
    assert after["channels"]["telegram"]["botToken"].startswith(
        "<REDACTED:sha256:"
    )
    assert after["meta"]["lastTouchedVersion"] == "2026.5.20"
    assert counts["files_changed"] == 1
    assert counts["secrets_redacted"] >= 1


def test_scrub_directory_dry_run_does_not_rewrite(tmp_path):
    baseline_file = tmp_path / "team_bot_a-openclaw.json"
    payload = {
        "channels": {"slack": {"botToken": "xoxb-real-slack-token-value-here"}}
    }
    _write(baseline_file, payload)

    counts = scrub_baselines.scrub_directory(tmp_path, dry_run=True)

    after = json.loads(baseline_file.read_text())
    assert after == payload
    assert counts["files_changed"] == 1
    assert counts["secrets_redacted"] >= 1


def test_scrub_directory_is_idempotent(tmp_path):
    baseline_file = tmp_path / "admin_bot-openclaw.json"
    _write(
        baseline_file,
        {"channels": {"telegram": {"botToken": "1111:AAEsecret-token-12345678901234567"}}},
    )
    scrub_baselines.scrub_directory(tmp_path, dry_run=False)
    first = baseline_file.read_text()

    counts2 = scrub_baselines.scrub_directory(tmp_path, dry_run=False)
    second = baseline_file.read_text()

    assert first == second
    assert counts2["files_changed"] == 0


def test_scrub_directory_handles_non_json_files(tmp_path):
    """Hash files / text baselines get string-pattern scrubbed."""
    txt = tmp_path / "shell-hashes.txt"
    txt.write_text(
        "SOUL.md  abc123\nAPI_KEY=sk-ant-realkeyvaluehere1234567890abcdefghij\n"
    )
    scrub_baselines.scrub_directory(tmp_path, dry_run=False)
    after = txt.read_text()
    assert "sk-ant-realkeyvaluehere1234567890abcdefghij" not in after
    assert "<REDACTED:sha256:" in after


def test_scrub_directory_leaves_clean_files_alone(tmp_path):
    txt = tmp_path / "shell-hashes.txt"
    txt.write_text("SOUL.md  abc123\n")
    counts = scrub_baselines.scrub_directory(tmp_path, dry_run=False)
    assert counts["files_changed"] == 0


def test_scrub_missing_dir_raises(tmp_path):
    with pytest.raises(SystemExit):
        scrub_baselines.scrub_directory(tmp_path / "nonexistent", dry_run=False)
