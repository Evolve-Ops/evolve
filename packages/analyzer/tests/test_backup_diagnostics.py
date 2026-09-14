"""tests/test_backup_diagnostics.py — backup error classifier tests."""

from __future__ import annotations

import sys
from pathlib import Path

_ANALYZER_DIR = Path(__file__).parent.parent
if str(_ANALYZER_DIR) not in sys.path:
    sys.path.insert(0, str(_ANALYZER_DIR))

import backup_diagnostics as bd  # noqa: E402


# Real-world stderr captured from the 2026-06-07 multi-bot incident —
# the exact text that sat in state.json::last_error for 2 days without
# triggering an alert.
_REAL_INCIDENT_STDERR = (
    "push failed: identity_sign: private key "
    "/Users/team_bot_a/.ssh/evolve-backup-team_bot_a "
    "contents do not match public\n"
    "git@github.com: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n"
    "\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists."
)


def test_classify_returns_none_for_empty_input() -> None:
    assert bd.classify(None) is None
    assert bd.classify("") is None


def test_classify_returns_none_for_unknown_error() -> None:
    assert bd.classify("totally unexpected git internal error #42") is None


def test_ssh_key_mismatch_matches_real_incident_stderr() -> None:
    """The 2026-06-07 incident error must classify as ssh_key_mismatch,
    NOT as the more generic deploy_key_unrecognized — the mismatch
    pattern appears earlier in stderr and takes precedence.
    """
    result = bd.classify(_REAL_INCIDENT_STDERR)
    assert result is not None
    assert result["cause_id"] == "ssh_key_mismatch"
    assert "doesn't match" in result["title"].lower()
    assert len(result["fix_steps"]) >= 2


def test_specificity_ordering_mismatch_beats_permission_denied() -> None:
    """A stderr containing both 'identity_sign … do not match' AND
    'Permission denied (publickey)' must match the more specific
    mismatch — the pub-key-unrecognised classifier would mis-diagnose
    the same incident.
    """
    text = (
        "identity_sign: private key contents do not match public\n"
        "Permission denied (publickey).\n"
    )
    assert bd.classify(text)["cause_id"] == "ssh_key_mismatch"


def test_deploy_key_unrecognized_without_mismatch() -> None:
    """Plain 'Permission denied (publickey)' with no mismatch line must
    classify as deploy_key_unrecognized.
    """
    text = (
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    )
    result = bd.classify(text)
    assert result is not None
    assert result["cause_id"] == "deploy_key_unrecognized"


def test_host_key_unverified() -> None:
    result = bd.classify("Host key verification failed.")
    assert result is not None
    assert result["cause_id"] == "host_key_unverified"


def test_dns_failure_resolver() -> None:
    result = bd.classify("ssh: Could not resolve hostname github.com")
    assert result is not None
    assert result["cause_id"] == "dns_failure"


def test_dns_failure_wins_over_publickey() -> None:
    """A stderr that contains both DNS failure AND a stale Permission
    denied (publickey) line must classify as DNS — the connectivity
    issue is the root cause; the publickey error is downstream noise.
    """
    text = (
        "ssh: Could not resolve hostname github.com\n"
        "Permission denied (publickey).\n"
    )
    assert bd.classify(text)["cause_id"] == "dns_failure"


def test_repo_not_found() -> None:
    result = bd.classify("ERROR: Repository not found.\nfatal: …")
    assert result is not None
    assert result["cause_id"] == "repo_not_found"


def test_auth_method_disabled() -> None:
    result = bd.classify("No supported authentication methods available")
    assert result is not None
    assert result["cause_id"] == "auth_method_disabled"


def test_classify_output_is_json_serialisable() -> None:
    """fix_steps must be a list (not tuple) so the dict can be jsonified
    by Flask without further marshalling.
    """
    import json
    result = bd.classify(_REAL_INCIDENT_STDERR)
    json.dumps(result)  # would raise on non-serialisable types
    assert isinstance(result["fix_steps"], list)


def test_known_cause_ids_matches_pattern_table() -> None:
    ids = bd.known_cause_ids()
    # Spot-check the ones that backup_signal subscribes to + UI
    # deep-links may key off.
    assert "ssh_key_mismatch" in ids
    assert "deploy_key_unrecognized" in ids
    assert "dns_failure" in ids
    assert "repo_not_found" in ids
    assert len(ids) == len(set(ids)), "cause_ids must be unique"
