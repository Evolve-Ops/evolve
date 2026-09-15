"""Tests for session_kinds — the one rule for "what KIND of session is this?".

Pins:
  1. The shared case table (tests/fixtures/session-kind-cases.json) — the SAME
     file the plugin's toolProfiles.test.mjs reads, so a rule change that lands
     on only one side of the language boundary reddens the other suite.
  2. The three things the old ``unknown`` conflated are now three named kinds:
     an unindexed session, a bare one-shot dispatch, and a channel session
     whose index row never recorded its channel.
  3. Classification is total: every key returns a kind from ALL_KINDS, and
     nothing ever returns "unknown".
  4. The TS mirror declares the same rule constants (a cheap structural check
     that catches a token added on one side only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import session_kinds as sk

REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "session-kind-cases.json"
TS_MIRROR = REPO_ROOT / "packages" / "plugin" / "src" / "tools" / "ToolProfiles.ts"


def _cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


# ── 1. the shared case table ─────────────────────────────────────────────────
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["key"] or "<empty>")
def test_shared_case_table(case: dict) -> None:
    assert sk.classify_session_kind(case["key"], case["channel"]) == (
        case["kind"], case["expect_channel"],
    )


def test_case_table_covers_every_kind_the_plugin_can_profile() -> None:
    """The table is the contract both sides read — it must exercise every kind
    that resolves to a non-default tool profile, or a profile could change
    with nothing to catch it."""
    seen = {c["kind"] for c in _cases()}
    assert {"user", "scheduled", "evolve_internal", "oneshot", "subagent", "other"} <= seen


# ── 2. the three kinds the old "unknown" conflated ───────────────────────────
def test_no_index_row_is_unindexed_not_unclassifiable() -> None:
    # Absence of an index row is an absence of evidence, not a classification.
    assert sk.classify_index_entry("agent:main:explicit:abc", None) == (sk.KIND_UNINDEXED, None)


def test_the_admin_ui_chat_drawer_is_a_user_session_not_a_oneshot() -> None:
    """``evo.proxy.derive_session_id`` dispatches the admin UI's chat drawer as
    ``--session-id admin-ui-<page>``. It is an explicit id but a live operator
    conversation, so it must NOT get a background tool profile."""
    for key in ("agent:main:explicit:admin-ui-alerts",
                "agent:main:explicit:admin-ui-anon-00000000"):
        assert sk.classify_index_entry(key, {}) == (sk.KIND_USER, "admin-ui")


def test_bare_explicit_key_is_a_oneshot_dispatch() -> None:
    # `openclaw agent --session-id <uuid>` — the shape every Evolve analyzer
    # dispatch lands as (app_audit_tier3._dispatch_via_oc mints a bare uuid).
    key = "agent:main:explicit:00000000-0000-4000-8000-000000000000"
    assert sk.classify_index_entry(key, {}) == (sk.KIND_ONESHOT, None)
    # An Evolve-tagged one, by contrast, names itself.
    tagged = "agent:main:explicit:evolve:tier-classifier:1788000000000"
    assert sk.classify_index_entry(tagged, {}) == (sk.KIND_EVOLVE_INTERNAL, None)


def test_channel_recovered_from_the_key_when_the_index_forgot_it() -> None:
    key = "agent:main:telegram:direct:@someone"
    assert sk.classify_index_entry(key, {}) == (sk.KIND_USER, "telegram")
    # …and the index row still wins when it has one.
    assert sk.classify_index_entry(key, {"route": {"channel": "telegram"}}) == (
        sk.KIND_USER, "telegram",
    )
    assert sk.classify_index_entry("agent:main:main:thread:1.2", {"lastChannel": "slack"}) == (
        sk.KIND_USER, "slack",
    )


# ── 3. totality ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", [
    "", "x", "a:b", "a:b:c", "agent:main:", "agent:main:main",
    "agent:main:explicit", "::::", "agent:main:cron",
])
def test_classification_is_total_and_never_says_unknown(key: str) -> None:
    kind, _channel = sk.classify_session_kind(key)
    assert kind in sk.ALL_KINDS
    assert kind != "unknown"


def test_a_non_dict_route_does_not_crash_the_entry_reader() -> None:
    assert sk.classify_index_entry("agent:main:main", {"route": "not-a-dict"})[0] == sk.KIND_OTHER
    assert sk.classify_index_entry("agent:main:main", {"lastChannel": 17})[0] == sk.KIND_OTHER


# ── 4. the TS mirror declares the same rule ──────────────────────────────────
def test_typescript_mirror_declares_the_same_rule_tokens() -> None:
    """Structural parity check on the mirrored classifier.

    The behavioural parity is the shared case table above (both suites run it).
    This catches the other half: a token added to the Python rule but not to
    the TS one would still pass the table if no case exercises it yet.
    """
    src = TS_MIRROR.read_text(encoding="utf-8")
    for tag in sk.EVOLVE_TAGS + sk.SCHEDULED_TAGS:
        assert f'"{tag}"' in src, f"TS mirror is missing the {tag!r} rule token"
    for route in sk.NON_CHANNEL_ROUTES:
        assert f'"{route}"' in src, f"TS mirror is missing the {route!r} non-channel route"
    for kind in sk.ALL_KINDS:
        assert f'"{kind}"' in src, f"TS mirror is missing the {kind!r} session kind"
    for prefix in sk.CONSOLE_SESSION_PREFIXES:
        assert f'"{prefix}"' in src, f"TS mirror is missing the {prefix!r} console prefix"
