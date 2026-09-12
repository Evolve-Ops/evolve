"""tests/test_intake_promote.py — GitHub promotion (injectable transport)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
for p in (str(_ADMIN_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)


def _network_with_intake_config(**overrides):
    base = {
        "owner": "evolve-ops",
        "repo": "evolve",
        "token_slot": "github_intake",
        "labels": {
            "bug": ["intake", "bug"],
            "feature": ["intake", "enhancement"],
        },
    }
    base.update(overrides)
    return {
        "sharedDir": "/tmp",
        "intake": {"github": base},
    }


def _make_intake():
    from evolve_admin.intake.envelope import Intake, IntakeContext

    return Intake(
        id="intake-20260514-d0d0",
        kind="bug",
        body="apps page is empty",
        context=IntakeContext(primary_bot="evo", active_bot="team_bot_a"),
    )


def test_promotion_config_missing_owner_returns_none():
    from evolve_admin.intake import promote

    assert promote.PromotionConfig.from_network({}) is None
    assert promote.PromotionConfig.from_network(
        {"intake": {"github": {"owner": "", "repo": "evolve"}}}
    ) is None


def test_promotion_config_parses_legacy_single_target():
    """v1 schema (owner/repo at top level) parses as a single 'default' target."""
    from evolve_admin.intake import promote

    cfg = promote.PromotionConfig.from_network(_network_with_intake_config())
    assert cfg is not None
    assert cfg.default_target_name == "default"
    assert cfg.target_names == ["default"]
    default = cfg.resolve()
    assert default.owner == "evolve-ops"
    assert default.repo == "evolve"
    assert default.labels_by_kind["bug"] == ["intake", "bug"]
    assert default.token_slot == "github_intake"


def test_promotion_config_parses_multi_target():
    """v2 schema (targets dict) yields multiple resolvable PromotionTargets."""
    from evolve_admin.intake import promote

    network = {
        "sharedDir": "/tmp",
        "intake": {
            "github": {
                "default": "evolve",
                "targets": {
                    "evolve": {
                        "owner": "evolve-ops",
                        "repo": "evolve",
                        "labels": {"bug": ["bug"]},
                        "token_slot": "github_intake",
                    },
                    "openclaw": {
                        "owner": "openclaw",
                        "repo": "openclaw",
                        "labels": {},
                        "token_slot": "github_intake_openclaw",
                    },
                },
            }
        },
    }
    cfg = promote.PromotionConfig.from_network(network)
    assert cfg is not None
    assert cfg.default_target_name == "evolve"
    assert set(cfg.target_names) == {"evolve", "openclaw"}

    default = cfg.resolve()
    assert default.name == "evolve"
    assert default.owner == "evolve-ops"
    assert default.labels_by_kind == {"bug": ["bug"]}

    oc = cfg.resolve("openclaw")
    assert oc.owner == "openclaw"
    assert oc.repo == "openclaw"
    assert oc.token_slot == "github_intake_openclaw"


def test_promotion_config_resolve_unknown_target_raises():
    """Unknown target name surfaces a helpful error listing valid choices."""
    from evolve_admin.intake import promote

    network = {
        "intake": {
            "github": {
                "default": "evolve",
                "targets": {
                    "evolve": {"owner": "x", "repo": "y"},
                    "openclaw": {"owner": "openclaw", "repo": "openclaw"},
                },
            }
        }
    }
    cfg = promote.PromotionConfig.from_network(network)
    assert cfg is not None
    with pytest.raises(promote.PromotionError) as exc_info:
        cfg.resolve("nonexistent")
    msg = str(exc_info.value)
    assert "nonexistent" in msg
    # Lists configured names so the operator knows what to retry with.
    assert "evolve" in msg and "openclaw" in msg


def test_promotion_config_default_falls_back_to_first_when_missing():
    """If ``default`` is omitted or points at an unknown name, the first
    declared target is used. Friendlier than refusing to parse."""
    from evolve_admin.intake import promote

    # default field omitted
    network_no_default = {
        "intake": {
            "github": {
                "targets": {
                    "evolve": {"owner": "x", "repo": "y"},
                    "openclaw": {"owner": "openclaw", "repo": "openclaw"},
                }
            }
        }
    }
    cfg = promote.PromotionConfig.from_network(network_no_default)
    assert cfg is not None
    assert cfg.default_target_name in {"evolve", "openclaw"}  # first declared

    # default field present but unmatched
    network_bad_default = {
        "intake": {
            "github": {
                "default": "ghost",
                "targets": {
                    "evolve": {"owner": "x", "repo": "y"},
                },
            }
        }
    }
    cfg2 = promote.PromotionConfig.from_network(network_bad_default)
    assert cfg2 is not None
    assert cfg2.default_target_name == "evolve"


def test_promotion_config_multi_target_skips_malformed_entries():
    """One bad target entry doesn't poison the others."""
    from evolve_admin.intake import promote

    network = {
        "intake": {
            "github": {
                "targets": {
                    "good":   {"owner": "x", "repo": "y"},
                    "noowner": {"repo": "y"},                # missing owner
                    "norepo":  {"owner": "z"},               # missing repo
                    "wrong":   "not a dict",                 # not a dict
                }
            }
        }
    }
    cfg = promote.PromotionConfig.from_network(network)
    assert cfg is not None
    assert cfg.target_names == ["good"]


def test_promote_no_config_raises(tmp_path):
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    with pytest.raises(promote.PromotionError, match="not configured"):
        promote.promote(
            ix,
            network={"sharedDir": str(tmp_path)},
            shared_dir=tmp_path,
            token="x",
        )


def test_promote_no_token_raises(tmp_path):
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    with pytest.raises(promote.PromotionError, match="no token"):
        promote.promote(
            ix,
            network=_network_with_intake_config(),
            shared_dir=tmp_path,
            token="",
        )


def test_promote_already_filed_raises(tmp_path):
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    ix.promotion.github_issue_url = "https://github.com/evolve-ops/evolve/issues/42"
    store.write_intake(ix, tmp_path)

    with pytest.raises(promote.PromotionError, match="already filed"):
        promote.promote(
            ix,
            network=_network_with_intake_config(),
            shared_dir=tmp_path,
            token="t0k3n",
        )


def test_promote_happy_path_with_stub_transport(tmp_path):
    """Stubs out HTTP; verifies request shape, transition, persisted promotion."""
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = json.loads(body.decode())
        return 201, {
            "number": 4242,
            "html_url": "https://github.com/evolve-ops/evolve/issues/4242",
        }

    updated = promote.promote(
        ix,
        network=_network_with_intake_config(),
        shared_dir=tmp_path,
        token="ghp_t0k3n",
        promoted_by="admin",
        transport=fake_transport,
    )

    # Request shape
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.github.com/repos/evolve-ops/evolve/issues"
    assert captured["headers"]["Authorization"] == "Bearer ghp_t0k3n"
    assert captured["headers"]["Accept"] == "application/vnd.github+json"
    assert "labels" in captured["payload"]
    assert captured["payload"]["labels"] == ["intake", "bug"]
    assert captured["payload"]["title"].startswith("[bug]")

    # Transition + persisted promotion
    assert updated.state == "filed"
    assert updated.promotion.github_issue_number == 4242
    assert updated.promotion.github_issue_url.endswith("/4242")
    assert updated.promotion.promoted_by == "admin"
    assert updated.promotion.body_sent  # not empty
    assert "transcript_dropped" not in updated.promotion.redactions_applied  # no transcript present

    # File now in filed/ subdir
    filed_path = store.intake_path(tmp_path, ix.id, subdir="filed")
    assert filed_path.exists()
    open_path = store.intake_path(tmp_path, ix.id, subdir="open")
    assert not open_path.exists()


def test_promote_github_error_raises_and_keeps_open(tmp_path):
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    def fake_transport(method, url, headers, body):
        return 422, {"message": "Validation Failed", "errors": [{"code": "missing"}]}

    with pytest.raises(promote.PromotionError, match="422"):
        promote.promote(
            ix,
            network=_network_with_intake_config(),
            shared_dir=tmp_path,
            token="t",
            transport=fake_transport,
        )

    # State unchanged
    located = store.find_intake(tmp_path, ix.id)
    assert located is not None
    found, _, subdir = located
    assert subdir == "open"
    assert found.state == "open"
    assert found.promotion.github_issue_url is None


def test_promote_404_with_matching_owner_hints_at_scope_mismatch(tmp_path):
    """When GitHub 404s the create-issue call AND the token's self-login
    matches the target owner, the only explanation is that the PAT can't
    see the repo (classic PAT without `repo` scope, or fine-grained PAT
    that excludes this repo). The error message must say so — the
    operator's instinct otherwise is to chase collaborator settings."""
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    def fake_transport(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "evolve-ops"}
        return 404, {"message": "Not Found"}

    with pytest.raises(promote.PromotionError) as exc:
        promote.promote(
            ix,
            network=_network_with_intake_config(),
            shared_dir=tmp_path,
            token="ghp_classic_no_repo_scope",
            transport=fake_transport,
        )
    msg = str(exc.value)
    assert "404" in msg
    assert "evolve-ops" in msg
    assert "repo" in msg.lower()
    assert "scope" in msg.lower()


def test_promote_404_with_mismatched_owner_hints_at_wrong_account(tmp_path):
    """When the token authenticates as a different account than the
    target owner, the 404 is "not a collaborator" — fix is to either
    issue the PAT from the owner's account or grant collaborator access."""
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    def fake_transport(method, url, headers, body):
        if url.endswith("/user"):
            return 200, {"login": "some-other-user"}
        return 404, {"message": "Not Found"}

    with pytest.raises(promote.PromotionError) as exc:
        promote.promote(
            ix,
            network=_network_with_intake_config(),
            shared_dir=tmp_path,
            token="ghp_other_user_pat",
            transport=fake_transport,
        )
    msg = str(exc.value)
    assert "404" in msg
    assert "some-other-user" in msg
    assert "evolve-ops" in msg
    assert "collaborator" in msg.lower()


def test_promote_404_with_revoked_token_hints_at_rotation(tmp_path):
    """When even /user returns 401 (token revoked/expired), surface
    that — rotation is the only fix, no scope tweak will help."""
    from evolve_admin.intake import promote, store

    ix = _make_intake()
    store.write_intake(ix, tmp_path)

    def fake_transport(method, url, headers, body):
        if url.endswith("/user"):
            return 401, {"message": "Bad credentials"}
        return 404, {"message": "Not Found"}

    with pytest.raises(promote.PromotionError) as exc:
        promote.promote(
            ix,
            network=_network_with_intake_config(),
            shared_dir=tmp_path,
            token="ghp_revoked",
            transport=fake_transport,
        )
    msg = str(exc.value).lower()
    assert "404" in msg
    assert "revoked" in msg or "expired" in msg or "rejected" in msg


def test_promote_includes_transcript_when_requested(tmp_path):
    from evolve_admin.intake import promote, store
    from evolve_admin.intake.envelope import Intake, IntakeContext

    ix = Intake(
        id="intake-20260514-aaaa",
        kind="bug",
        body="thing broken",
        context=IntakeContext(
            recent_turns_excerpt=[{"role": "user", "text": "hello"}],
        ),
    )
    store.write_intake(ix, tmp_path)

    captured = {}

    def fake_transport(method, url, headers, body):
        captured["payload"] = json.loads(body.decode())
        return 201, {"number": 1, "html_url": "https://x/y/1"}

    updated = promote.promote(
        ix,
        network=_network_with_intake_config(),
        shared_dir=tmp_path,
        token="t",
        include_transcript=True,
        transport=fake_transport,
    )
    assert "hello" in captured["payload"]["body"]
    assert "transcript_included" in updated.promotion.redactions_applied
