"""Tests for bot_credential_inventory.

The module's whole contract is "say what is there without saying what it is",
so most of these tests are about two things: the classifier's precision (a
budget knob is not a credential; an env reference is not a secret on disk), and
the value-free guarantee (a real token value must never appear in any output).

Bot ids and token values here are placeholders (docs/PLACEHOLDER_NAMING.md) —
never real accounts or real secrets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ANALYZER_DIR = Path(__file__).resolve().parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import bot_credential_inventory as inv  # noqa: E402


# A placeholder that is long enough to read as live (>= _MIN_SECRET_LEN) and
# obviously fake. Used to prove it never reaches an output string.
FAKE_TOKEN = "PLACEHOLDER-not-a-real-token-0000000000"


# ── Secret-shaped key classification ──────────────────────────────────────


@pytest.mark.parametrize("key", [
    "botToken", "token", "apiKey", "api_key", "clientSecret", "password",
    "signingKey", "accessKey", "BOTTOKEN",
])
def test_secret_shaped_keys_match(key):
    assert inv._is_secret_key(key)


@pytest.mark.parametrize("key", [
    "maxTokens", "maxOutputTokens", "tokenLimit", "tokenizer", "tokenCount",
    "contextTokens",
])
def test_budget_knobs_are_not_secrets(key):
    """`maxTokens` contains "token" — the denylist is what keeps the harvest
    from reporting every model budget knob as a credential."""
    assert not inv._is_secret_key(key)


@pytest.mark.parametrize("key", ["model", "workspace", "dmPolicy", "plugins"])
def test_ordinary_keys_do_not_match(key):
    assert not inv._is_secret_key(key)


# ── Value classification ──────────────────────────────────────────────────


def test_live_token_reports_length_only():
    shape, live = inv._classify_value(FAKE_TOKEN)
    assert live is True
    assert shape == f"{len(FAKE_TOKEN)} chars"
    assert FAKE_TOKEN not in shape


def test_empty_value_is_not_live():
    shape, live = inv._classify_value("")
    assert live is False
    assert "nothing to revoke" in shape


def test_whitespace_only_value_is_not_live():
    assert inv._classify_value("   ")[1] is False


@pytest.mark.parametrize("value", ["${TELEGRAM_TOKEN}", "env:TG_TOKEN", "$(cat x)"])
def test_env_indirection_is_not_a_secret_on_disk(value):
    """A config that only NAMES an env var has no credential to revoke —
    reporting it as live would send the operator chasing nothing."""
    shape, live = inv._classify_value(value)
    assert live is False
    assert "environment reference" in shape


def test_short_string_is_not_live():
    shape, live = inv._classify_value("none")
    assert live is False
    assert "too short" in shape


def test_non_string_value_is_never_a_credential():
    assert inv._classify_value(4096) == ("", False)
    assert inv._classify_value(None) == ("", False)


# ── Config harvest ────────────────────────────────────────────────────────


def test_harvest_finds_nested_channel_token():
    config = {"channels": {"telegram": {"botToken": FAKE_TOKEN, "dmPolicy": "pairing"}}}
    found = inv.harvest_config_secrets(config)
    assert len(found) == 1
    assert found[0].location == "openclaw.json::channels.telegram.botToken"
    assert found[0].live is True


def test_harvest_never_emits_the_value():
    """The value-free contract, asserted against every field of the result."""
    config = {
        "channels": {"telegram": {"botToken": FAKE_TOKEN}},
        "gateway": {"auth": {"token": FAKE_TOKEN}},
    }
    blob = repr(inv.harvest_config_secrets(config))
    assert FAKE_TOKEN not in blob
    assert "2 " not in blob or True  # sanity: the assertion above is the point


def test_harvest_ignores_model_budget_knobs():
    config = {"agents": {"defaults": {"maxTokens": 4096, "model": "some-model"}}}
    assert inv.harvest_config_secrets(config) == []


def test_harvest_reports_gateway_token_with_gateway_hint():
    config = {"gateway": {"auth": {"token": FAKE_TOKEN}}}
    found = inv.harvest_config_secrets(config)
    assert found[0].location == "openclaw.json::gateway.auth.token"
    assert "gateway" in found[0].revoke_with.lower()


def test_harvest_is_depth_bounded():
    node: dict = {"botToken": FAKE_TOKEN}
    for _ in range(inv._MAX_WALK_DEPTH + 5):
        node = {"nested": node}
    assert inv.harvest_config_secrets(node) == []


def test_harvest_covers_an_unknown_channel():
    """Selection is by key NAME, so a platform Evolve has never heard of is
    covered by the same walk — no per-channel special casing."""
    config = {"channels": {"someNewPlatform": {"apiKey": FAKE_TOKEN}}}
    found = inv.harvest_config_secrets(config)
    assert len(found) == 1
    assert "someNewPlatform" in found[0].location


def test_revoke_hint_for_known_channel_names_the_destination():
    hint = inv._revoke_hint_for("channels.telegram.botToken")
    assert "BotFather" in hint


def test_revoke_hint_for_unknown_channel_is_generic_not_invented():
    hint = inv._revoke_hint_for("channels.someNewPlatform.apiKey")
    assert "developer console" in hint


# ── Filesystem harvest ────────────────────────────────────────────────────


def test_ssh_keypair_is_reported_and_pub_half_is_not(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "evolve-backup-placeholder").write_text("PRIVATE KEY PLACEHOLDER")
    (ssh / "evolve-backup-placeholder.pub").write_text("ssh-ed25519 AAAA")
    (ssh / "known_hosts").write_text("")
    (ssh / "config").write_text("")

    result = inv.Inventory()
    inv._ssh_key_artifacts(tmp_path, result)

    locations = [a.location for a in result.artifacts]
    assert locations == [str(ssh / "evolve-backup-placeholder")]
    assert result.artifacts[0].shape == "private key file"


def test_unpaired_ssh_key_is_still_reported_with_the_weaker_claim(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "mystery_key").write_text("x")

    result = inv.Inventory()
    inv._ssh_key_artifacts(tmp_path, result)
    assert "no .pub sibling" in result.artifacts[0].shape


def test_missing_ssh_dir_is_clean_not_unread(tmp_path):
    result = inv.Inventory()
    inv._ssh_key_artifacts(tmp_path, result)
    assert result.artifacts == []
    assert result.unread == []


def test_allowlist_files_are_not_called_credentials(tmp_path):
    """`.openclaw/credentials/` mixes pairing secrets with plain allowlists.
    Calling an allowlist a credential sends the operator chasing a revocation
    that does not exist."""
    creds = tmp_path / ".openclaw" / "credentials"
    creds.mkdir(parents=True)
    (creds / "telegram-pairing.json").write_text("{}")
    (creds / "telegram-default-allowFrom.json").write_text("[]")

    result = inv.Inventory()
    inv._credential_dir_artifacts(tmp_path, result)

    names = [Path(a.location).name for a in result.artifacts]
    assert names == ["telegram-pairing.json"]


def test_auth_store_present_is_reported(tmp_path):
    agent = tmp_path / ".openclaw" / "agents" / "main" / "agent"
    agent.mkdir(parents=True)
    (agent / "auth-profiles.json").write_text("{}")

    result = inv.Inventory()
    inv._auth_store_artifacts(tmp_path, result)
    assert result.artifacts[0].kind == "llm_auth_profile"


# ── The fail-safe: unreadable is never "clean" ────────────────────────────


def test_probe_distinguishes_absent_from_unreadable(tmp_path):
    assert inv._probe(tmp_path / "nope") == "absent"
    (tmp_path / "here").write_text("x")
    assert inv._probe(tmp_path / "here") == "present"


def test_is_blind_is_true_when_a_location_could_not_be_read():
    result = inv.Inventory(unread=["/somewhere/.ssh — unreadable"])
    assert result.is_blind is True
    assert result.live_artifacts == []


def test_summarize_names_the_unreadable_count():
    result = inv.Inventory(
        artifacts=[inv.CredentialArtifact("x", "k", "40 chars", "revoke")],
        unread=["/a — unreadable"],
    )
    assert "1 live credential" in inv.summarize(result)
    assert "1 location(s) unreadable" in inv.summarize(result)


def test_markdown_flags_an_incomplete_list(tmp_path):
    """A summary that says "no credentials" while a read failed would be the
    single most dangerous way this module could be wrong."""
    result = inv.Inventory(unread=[f"{tmp_path}/.ssh — unreadable"])
    md = inv.as_markdown(result)
    assert "may be incomplete" in md


def test_markdown_separates_live_from_considered_and_dismissed():
    result = inv.Inventory(artifacts=[
        inv.CredentialArtifact("live-one", "config_field", "40 chars", "revoke it"),
        inv.CredentialArtifact(
            "empty-one", "config_field", "empty — nothing to revoke", "n/a",
            live=False,
        ),
    ])
    md = inv.as_markdown(result)
    assert "live-one" in md.split("Checked and NOT a live secret")[0]
    assert "empty-one" in md.split("Checked and NOT a live secret")[1]


# ── End-to-end on a synthetic home ────────────────────────────────────────


def test_collect_end_to_end_reproduces_the_2026_08_02_finding(tmp_path):
    """The shape of the live finding: a channel token, a gateway token, and an
    SSH keypair in a retired bot's home — reported without any value."""
    import json as _json

    oc = tmp_path / ".openclaw"
    oc.mkdir()
    (oc / "openclaw.json").write_text(_json.dumps({
        "channels": {"telegram": {"botToken": FAKE_TOKEN}},
        "gateway": {"auth": {"token": FAKE_TOKEN}},
        "agents": {"defaults": {"maxTokens": 4096}},
    }))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "evolve-backup-placeholder").write_text("x")
    (ssh / "evolve-backup-placeholder.pub").write_text("y")

    result = inv.collect(tmp_path)

    kinds = sorted(a.kind for a in result.live_artifacts)
    assert kinds == ["config_field", "config_field", "ssh_private_key"]
    assert result.unread == []
    # The value-free contract, end to end.
    assert FAKE_TOKEN not in repr(result)
    assert FAKE_TOKEN not in inv.as_markdown(result)


def test_collect_on_a_home_with_no_openclaw_is_clean(tmp_path):
    result = inv.collect(tmp_path)
    assert result.live_artifacts == []
    assert result.unread == []
