"""tests/test_intake_evo_handler.py — `evo bug`/`evo feature`/`evo intake` handlers."""

from __future__ import annotations

import sys
from pathlib import Path

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


def _network(tmp_path: Path) -> dict:
    return {"sharedDir": str(tmp_path), "primary": "evo"}


def test_evo_bug_with_no_body_returns_usage(tmp_path):
    from evolve_admin.evo.handlers.intake import render_bug

    r = render_bug(role="primary", bot_id="evo", args="", network=_network(tmp_path))
    body = r.direct_send_message or ""
    assert "Tell me what to capture" in body


def test_evo_bug_captures_and_returns_id(tmp_path):
    from evolve_admin.evo.handlers.intake import render_bug
    from evolve_admin.intake import store

    r = render_bug(
        role="primary",
        bot_id="team_bot_a",
        args="when I do X, Y happens",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "Captured" in body
    # An id was minted and an intake exists
    intakes = list(store.iter_intakes(tmp_path))
    assert len(intakes) == 1
    ix = intakes[0]
    assert ix.kind == "bug"
    assert ix.body == "when I do X, Y happens"
    assert ix.context.active_bot == "team_bot_a"
    assert ix.context.primary_bot == "evo"
    assert ix.id in body


def test_evo_feature_marks_kind_feature(tmp_path):
    from evolve_admin.evo.handlers.intake import render_feature
    from evolve_admin.intake import store

    render_feature(
        role="primary",
        bot_id="team_bot_a",
        args="would love a dark theme",
        network=_network(tmp_path),
    )
    intakes = list(store.iter_intakes(tmp_path))
    assert intakes[0].kind == "feature"


def test_evo_intake_list_when_empty(tmp_path):
    from evolve_admin.evo.handlers.intake import render

    r = render(role="primary", bot_id="evo", args="", network=_network(tmp_path))
    body = r.direct_send_message or ""
    assert "Nothing in the queue" in body


def test_evo_intake_list_shows_open_only(tmp_path):
    from evolve_admin.evo.handlers.intake import render
    from evolve_admin.intake.envelope import Intake
    from evolve_admin.intake import store

    o = Intake(id="intake-20260514-1111", kind="bug", body="open one")
    c = Intake(id="intake-20260514-2222", kind="bug", body="closed one", state="closed")
    for ix in (o, c):
        store.write_intake(ix, tmp_path)

    r = render(role="primary", bot_id="evo", args="list", network=_network(tmp_path))
    body = r.direct_send_message or ""
    assert "open one" in body
    assert "closed one" not in body
    assert "1 open" in body


def test_evo_intake_list_filters_by_kind(tmp_path):
    from evolve_admin.evo.handlers.intake import render
    from evolve_admin.intake.envelope import Intake
    from evolve_admin.intake import store

    b = Intake(id="intake-20260514-aaaa", kind="bug", body="bug body")
    f = Intake(id="intake-20260514-bbbb", kind="feature", body="feat body")
    for ix in (b, f):
        store.write_intake(ix, tmp_path)

    r = render(role="primary", bot_id="evo", args="list feature", network=_network(tmp_path))
    body = r.direct_send_message or ""
    assert "feat body" in body
    assert "bug body" not in body


def test_evo_intake_promote_no_config(tmp_path):
    from evolve_admin.evo.handlers.intake import render
    from evolve_admin.intake.envelope import Intake
    from evolve_admin.intake import store

    ix = Intake(id="intake-20260514-aaaa", kind="bug", body="x")
    store.write_intake(ix, tmp_path)
    r = render(
        role="primary",
        bot_id="evo",
        args=f"promote {ix.id}",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "isn't set up yet" in body or "evolve-admin intake configure" in body


def test_evo_intake_promote_missing_id(tmp_path):
    from evolve_admin.evo.handlers.intake import render

    r = render(role="primary", bot_id="evo", args="promote nope", network=_network(tmp_path))
    body = r.direct_send_message or ""
    assert "No intake with id" in body


def test_parse_capture_args_post_flag():
    from evolve_admin.evo.handlers.intake import _parse_capture_args

    # New three-tuple shape: (body, post, target_name)
    assert _parse_capture_args("foo bar") == ("foo bar", False, None)
    assert _parse_capture_args("--post foo bar") == ("foo bar", True, None)
    assert _parse_capture_args("foo bar --post") == ("foo bar", True, None)
    assert _parse_capture_args("--post") == ("", True, None)
    assert _parse_capture_args("") == ("", False, None)


def test_parse_capture_args_with_target():
    """--to <name> can appear in either order with --post and the body."""
    from evolve_admin.evo.handlers.intake import _parse_capture_args

    # --to leading
    assert _parse_capture_args("--to openclaw foo bar") == ("foo bar", False, "openclaw")
    # --to trailing
    assert _parse_capture_args("foo bar --to openclaw") == ("foo bar", False, "openclaw")
    # --to + --post in various combos
    assert _parse_capture_args("--post --to openclaw foo") == ("foo", True, "openclaw")
    assert _parse_capture_args("foo --to openclaw --post") == ("foo", True, "openclaw")
    assert _parse_capture_args("--to openclaw --post foo") == ("foo", True, "openclaw")
    # bare --to (no body left): treated as missing body
    assert _parse_capture_args("--to openclaw") == ("", False, "openclaw")


def test_parse_promote_args():
    """`<id>` with optional `--to <name>` in either order."""
    from evolve_admin.evo.handlers.intake import _parse_promote_args

    assert _parse_promote_args("intake-20260522-abcd") == ("intake-20260522-abcd", None)
    assert _parse_promote_args("intake-1 --to openclaw") == ("intake-1", "openclaw")
    assert _parse_promote_args("--to openclaw intake-1") == ("intake-1", "openclaw")
    assert _parse_promote_args("") == ("", None)
    # --to without a value: ignored (caller surfaces usage hint via empty id)
    assert _parse_promote_args("--to") == ("--to", None)


def test_evo_bug_post_failure_surfaces_intake_id(tmp_path):
    """If --post fails, the captured id must appear in the failure message
    so the user knows the report wasn't lost and can retry."""
    from evolve_admin.evo.handlers.intake import render_bug

    # No intake.github config → promotion fails fast
    r = render_bug(
        role="primary",
        bot_id="team_bot_a",
        args="--post when I do X, Y happens",
        network=_network(tmp_path),
    )
    body = r.direct_send_message or ""
    assert "isn't set up yet" in body or "intake configure" in body
    # The hint pointing the user at the captured id must be present
    assert "evo intake promote intake-" in body
    assert "Captured as `intake-" in body


def test_evo_bug_post_success_files_to_github(tmp_path):
    """End-to-end --post happy path: capture + promote via stubbed transport."""
    from evolve_admin.evo.handlers.intake import render_bug
    from evolve_admin.intake import promote as _promote
    from evolve_admin.intake import store as _store

    # Configure intake.github so PromotionConfig.from_network returns one
    network = _network(tmp_path)
    network["intake"] = {
        "github": {"owner": "evolve-ops", "repo": "evolve", "token_slot": "github_intake"}
    }

    # Stub the keystore lookup and the HTTP transport
    import evolve_admin.evo.handlers.intake as ih

    class _FakeKeystoreManager:
        def __init__(self, *a, **kw):
            pass

        def get_value(self, name):
            return "ghp_fake"

    # Patch lazy import target — handler does `from ...keystore import KeystoreManager`
    import evolve_admin.keystore as ks_mod
    real_km = ks_mod.KeystoreManager
    ks_mod.KeystoreManager = _FakeKeystoreManager  # type: ignore[assignment]

    def fake_tx(method, url, headers, body):
        return 201, {"number": 99, "html_url": "https://github.com/evolve-ops/evolve/issues/99"}

    real_default = _promote._default_transport
    _promote._default_transport = fake_tx  # type: ignore[assignment]
    try:
        r = render_bug(
            role="primary",
            bot_id="team_bot_a",
            args="--post fast path works",
            network=network,
        )
    finally:
        _promote._default_transport = real_default  # type: ignore[assignment]
        ks_mod.KeystoreManager = real_km  # type: ignore[assignment]

    body = r.direct_send_message or ""
    assert "Filed" in body
    assert "issues/99" in body
    # Intake exists in filed state
    intakes = list(_store.iter_intakes(tmp_path, state="filed"))
    assert len(intakes) == 1
    assert intakes[0].promotion.github_issue_url.endswith("/99")


def test_capture_auto_fills_evolve_version(tmp_path):
    """git_commit may be None outside a git tree; evolve_version should
    always populate since EVOLVE_VERSION is a module constant."""
    from evolve_admin.evo.handlers.intake import render_bug
    from evolve_admin.intake import store as _store

    render_bug(
        role="primary",
        bot_id="team_bot_a",
        args="thing broken",
        network=_network(tmp_path),
    )
    ix = next(iter(_store.iter_intakes(tmp_path)))
    assert ix.context.evolve_version is not None
    # In CI this may be None; locally it'll be a short sha. Either is fine.
    # The shape we care about: it doesn't crash and the field is filled
    # when available.
