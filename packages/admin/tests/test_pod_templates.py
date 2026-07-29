"""
test_pod_templates.py — Unit tests for the pod-template framework (Bite 1).

Covers loader, validator, and extractor. Three fixture sources:
  1. Tmp-dir pod.yaml files synthesised per-test — loader/validator paths.
  2. Synthetic ``network.json`` dicts — the extractor's *pure* core, with no
     filesystem and (critically) no ``/Users/`` access.
  3. Lightweight stand-in bot-template objects — the matcher, decoupled from
     the real bot-template loader.

The two invariants under the microscope:
  * membership is read ONLY from network.json (never a /Users scan), and
  * no secret ever lands in a captured pod-template.

Run with:
    python3 -m pytest packages/admin/tests/test_pod_templates.py -v
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.pod_templates import (  # noqa: E402
    POD_TEMPLATE_SCHEMA_VERSION,
    TYPED_ARTIFACT_TYPE,
    BotEvidence,
    PodTemplateError,
    PodTemplateExtractError,
    PodTemplateNotFoundError,
    PodTemplateValidationError,
    bot_evidence_from_files,
    extract_pod_template,
    find_secrets,
    is_secret_key,
    list_pod_templates,
    load_pod_template,
    match_bot_template,
    pod_members,
    pod_template_from_dict,
    validate_pod_template,
    value_looks_secret,
    wrap_typed_artifact,
    write_pod_template,
)


# ── Helpers / fixtures ──────────────────────────────────────────────────────────


def _write_pod_yaml(root: Path, name: str, body: dict) -> Path:
    tdir = root / name
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "pod.yaml").write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return tdir


_VALID_BODY = {
    "schema_version": 1,
    "name": "home-pod",
    "display_name": "Home Pod",
    "description": "A captured household pod.",
    "pod": {
        "release_mode": "canary",
        "model_tiers": {"rungs": [{"name": "fast"}]},
        "integrations": {"github": {"scope": "pod"}},
    },
    "bots": [
        {"bot_id": "evolve", "bot_template": None, "channel": "telegram", "role": "primary"},
        {"bot_id": "member_bot", "bot_template": "morning-briefing", "channel": "telegram", "role": "member"},
    ],
}


# Stand-in bot-template objects for the matcher (duck-typed: .name,
# .applications[].app_id/.name, .skills[].id).
@dataclass
class _App:
    app_id: str = ""
    name: str = ""


@dataclass
class _Skill:
    id: str = ""


@dataclass
class _Tpl:
    name: str
    applications: list = field(default_factory=list)
    skills: list = field(default_factory=list)


# ── Loader ──────────────────────────────────────────────────────────────────────


def test_load_valid_pod_template(tmp_path):
    _write_pod_yaml(tmp_path, "home-pod", _VALID_BODY)
    t = load_pod_template("home-pod", templates_dir=tmp_path)
    assert t.name == "home-pod"
    assert t.display_name == "Home Pod"
    assert t.schema_version == 1
    assert t.pod.release_mode == "canary"
    assert t.pod.model_tiers == {"rungs": [{"name": "fast"}]}
    assert {b.bot_id for b in t.bots} == {"evolve", "member_bot"}
    member_bot = next(b for b in t.bots if b.bot_id == "member_bot")
    assert member_bot.bot_template == "morning-briefing"
    assert member_bot.role == "member"


def test_load_missing_dir_raises_not_found(tmp_path):
    with pytest.raises(PodTemplateNotFoundError):
        load_pod_template("nope", templates_dir=tmp_path)


def test_load_missing_manifest_raises_not_found(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(PodTemplateNotFoundError):
        load_pod_template("empty", templates_dir=tmp_path)


def test_load_malformed_yaml_raises(tmp_path):
    tdir = tmp_path / "broken"
    tdir.mkdir()
    (tdir / "pod.yaml").write_text("key: [unterminated\n", encoding="utf-8")
    with pytest.raises(PodTemplateError):
        load_pod_template("broken", templates_dir=tmp_path)


def test_load_non_mapping_top_level_raises(tmp_path):
    tdir = tmp_path / "list-top"
    tdir.mkdir()
    (tdir / "pod.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(PodTemplateError):
        load_pod_template("list-top", templates_dir=tmp_path)


def test_list_pod_templates(tmp_path):
    _write_pod_yaml(tmp_path, "a-pod", _VALID_BODY)
    _write_pod_yaml(tmp_path, "b-pod", _VALID_BODY)
    (tmp_path / "not-a-template").mkdir()  # no pod.yaml → skipped
    assert list_pod_templates(templates_dir=tmp_path) == ["a-pod", "b-pod"]


def test_list_pod_templates_missing_root(tmp_path):
    assert list_pod_templates(templates_dir=tmp_path / "does-not-exist") == []


def test_bot_id_required(tmp_path):
    body = dict(_VALID_BODY)
    body["bots"] = [{"channel": "telegram"}]  # no bot_id
    _write_pod_yaml(tmp_path, "bad", body)
    with pytest.raises(PodTemplateError):
        load_pod_template("bad", templates_dir=tmp_path)


def test_dirname_wins_over_raw_name(tmp_path):
    body = dict(_VALID_BODY)
    body["name"] = "different-in-file"
    _write_pod_yaml(tmp_path, "home-pod", body)
    t = load_pod_template("home-pod", templates_dir=tmp_path)
    assert t.name == "home-pod"


def test_forward_compat_extra_pod_keys_preserved():
    raw = {
        "schema_version": 1,
        "name": "x",
        "pod": {"release_mode": "canary", "future_field": {"k": "v"}},
        "bots": [],
    }
    t = pod_template_from_dict(raw, name="x")
    assert t.pod.extra == {"future_field": {"k": "v"}}
    assert t.to_payload()["pod"]["future_field"] == {"k": "v"}


# ── Round-trip + typed-artifact envelope ────────────────────────────────────────


def test_payload_round_trip():
    t = pod_template_from_dict(_VALID_BODY, name="home-pod")
    again = pod_template_from_dict(t.to_payload(), name="home-pod")
    assert again.to_payload() == t.to_payload()


def test_to_yaml_is_loadable(tmp_path):
    t = pod_template_from_dict(_VALID_BODY, name="home-pod")
    (tmp_path / "home-pod").mkdir()
    (tmp_path / "home-pod" / "pod.yaml").write_text(t.to_yaml(), encoding="utf-8")
    reloaded = load_pod_template("home-pod", templates_dir=tmp_path)
    assert reloaded.to_payload() == t.to_payload()


def test_typed_artifact_envelope_shape():
    t = pod_template_from_dict(_VALID_BODY, name="home-pod")
    env = wrap_typed_artifact(t)
    assert set(env.keys()) == {"type", "schema_version", "payload", "signature"}
    assert env["type"] == TYPED_ARTIFACT_TYPE == "pod_template"
    assert env["schema_version"] == POD_TEMPLATE_SCHEMA_VERSION
    assert env["signature"] is None  # Bite 1 does not sign
    # The body IS the payload — forward-compat with design-multi-pod §5.
    assert env["payload"] == t.to_payload()


def test_typed_artifact_accepts_supplied_signature():
    t = pod_template_from_dict(_VALID_BODY, name="home-pod")
    env = t.wrap_typed_artifact(signature="abc")
    assert env["signature"] == "abc"


# ── Validator ───────────────────────────────────────────────────────────────────


def test_validate_clean():
    t = pod_template_from_dict(_VALID_BODY, name="home-pod")
    r = validate_pod_template(t)
    assert r.ok
    assert r.errors == []


def test_validate_bad_name():
    body = dict(_VALID_BODY)
    t = pod_template_from_dict(body, name="Bad_Name!")
    r = validate_pod_template(t)
    assert not r.ok
    assert any("must match" in e for e in r.errors)


def test_validate_duplicate_bot_ids():
    body = dict(_VALID_BODY)
    body["bots"] = [
        {"bot_id": "dup", "role": "primary"},
        {"bot_id": "dup", "role": "member"},
    ]
    t = pod_template_from_dict(body, name="home-pod")
    r = validate_pod_template(t)
    assert not r.ok
    assert any("more than once" in e for e in r.errors)


def test_validate_invalid_bot_id():
    body = dict(_VALID_BODY)
    body["bots"] = [{"bot_id": "Has Spaces", "role": "primary"}]
    t = pod_template_from_dict(body, name="home-pod")
    r = validate_pod_template(t)
    assert not r.ok


def test_validate_no_primary_warns():
    body = dict(_VALID_BODY)
    body["bots"] = [{"bot_id": "a", "role": "member"}]
    t = pod_template_from_dict(body, name="home-pod")
    r = validate_pod_template(t)
    assert r.ok
    assert any("no bot has role 'primary'" in w for w in r.warnings)


def test_validate_multiple_primaries_warns():
    body = dict(_VALID_BODY)
    body["bots"] = [
        {"bot_id": "a", "role": "primary"},
        {"bot_id": "b", "role": "primary"},
    ]
    t = pod_template_from_dict(body, name="home-pod")
    r = validate_pod_template(t)
    assert r.ok
    assert any("primary" in w for w in r.warnings)


def test_validate_empty_bots_warns():
    body = {"schema_version": 1, "name": "x", "pod": {}, "bots": []}
    t = pod_template_from_dict(body, name="x")
    r = validate_pod_template(t)
    assert r.ok
    assert any("no bots" in w for w in r.warnings)


def test_validate_unknown_release_mode_warns():
    body = dict(_VALID_BODY)
    body["pod"] = {"release_mode": "yolo"}
    t = pod_template_from_dict(body, name="home-pod")
    r = validate_pod_template(t)
    assert r.ok
    assert any("release_mode" in w for w in r.warnings)


def test_validate_future_schema_version_warns():
    body = dict(_VALID_BODY)
    body["schema_version"] = 99
    t = pod_template_from_dict(body, name="home-pod")
    r = validate_pod_template(t)
    assert any("newer than this" in w for w in r.warnings)


def test_validate_secret_in_body_is_error():
    body = {
        "schema_version": 1,
        "name": "leaky",
        "pod": {"integrations": {"github": {"scope": "pod", "token": "ghp_" + "a" * 36}}},
        "bots": [{"bot_id": "a", "role": "primary"}],
    }
    t = pod_template_from_dict(body, name="leaky")
    r = validate_pod_template(t)
    assert not r.ok
    assert any("secret" in e.lower() for e in r.errors)


def test_validation_report_raises():
    body = dict(_VALID_BODY)
    t = pod_template_from_dict(body, name="Bad Name")
    r = validate_pod_template(t)
    with pytest.raises(PodTemplateValidationError):
        r.raise_if_invalid()


# ── Secret detection primitives ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key,expected",
    [
        ("token", True),
        ("api_key", True),
        ("admin_passphrase", True),
        ("client_secret", True),
        ("my_pat", True),
        ("auth_token", True),
        # Negatives — must NOT flag legitimate shape fields.
        ("model_keys", False),
        ("pattern", False),
        ("compatible", False),
        ("scope", False),
        ("release_mode", False),
        ("bot_template", False),
    ],
)
def test_is_secret_key(key, expected):
    assert is_secret_key(key) is expected


@pytest.mark.parametrize(
    "val,expected",
    [
        ("ghp_" + "x" * 36, True),
        ("sk-ant-" + "y" * 40, True),
        ("xoxb-123456789012-abcdef", True),
        ("AKIA" + "A" * 16, True),
        ("postgres://user:SuperSecret123@host/db", True),  # connection-string creds
        ("redis://admin:p4ss@10.0.0.1:6379", True),
        ("just a normal description", False),
        ("telegram", False),
        ("https://vps.example.ts.net:5050", False),  # URL w/o embedded creds
    ],
)
def test_value_looks_secret(val, expected):
    assert value_looks_secret(val) is expected


def test_extract_scrubs_connection_string_in_overrides():
    net = {"bots": {"a": {"role": "primary", "user": "a"}}}
    ev = {"a": BotEvidence(overrides={"db": "postgres://u:p4ssw0rd@h/db", "tz": "UTC"})}
    t = extract_pod_template(net, name="p", evidence=ev, candidates=[])
    assert "p4ssw0rd" not in t.to_yaml()
    assert t.bots[0].overrides.get("tz") == "UTC"  # non-secret override survives


def test_find_secrets_nested():
    blob = {"a": {"b": [{"password": "hunter2"}]}, "ok": "fine"}
    hits = find_secrets(blob)
    assert hits
    assert hits[0][0] == "$.a.b[0].password"


def test_find_secrets_empty_secret_key_not_flagged():
    # A secret-named key with an empty value is a placeholder shape, not a leak.
    assert find_secrets({"token": ""}) == []
    assert find_secrets({"token": None}) == []


# ── Extractor: membership (network.json ONLY) ───────────────────────────────────


def test_membership_from_bots_dict():
    net = {"bots": {"a": {"role": "primary"}, "b": {"role": "member"}}}
    assert [bid for bid, _ in pod_members(net)] == ["a", "b"]


def test_membership_merges_pod_bots():
    net = {
        "bots": {"a": {"role": "primary"}},
        "pod": {"bots": {"b": {"role": "member"}}},
    }
    assert {bid for bid, _ in pod_members(net)} == {"a", "b"}


def test_membership_excludes_planned_bots():
    net = {"bots": {"a": {"role": "primary"}, "planned": {"purpose": "later"}}}
    assert [bid for bid, _ in pod_members(net)] == ["a"]


def test_membership_ignores_non_dict_sources():
    net = {"bots": None, "pod": {"bots": "garbage"}}
    assert pod_members(net) == []


def test_extract_empty_roster_raises():
    with pytest.raises(PodTemplateExtractError):
        extract_pod_template({"bots": {}}, name="empty")


def test_membership_non_dict_pod_block_does_not_crash():
    # A malformed network.json with a non-dict 'pod' must not raise a raw
    # AttributeError out of the membership reader.
    net = {"bots": {"a": {"role": "primary"}}, "pod": ["garbage", "list"]}
    assert [bid for bid, _ in pod_members(net)] == ["a"]


def test_membership_non_dict_network_returns_empty():
    assert pod_members(["not", "a", "dict"]) == []  # type: ignore[arg-type]


def test_extract_non_dict_network_raises_clean():
    with pytest.raises(PodTemplateExtractError):
        extract_pod_template(["nope"], name="x")  # type: ignore[arg-type]


def test_extract_malformed_pod_block_raises_clean():
    # 'pod' is a list (malformed) and there are no top-level bots → a clean
    # PodTemplateExtractError, never an AttributeError.
    with pytest.raises(PodTemplateExtractError):
        extract_pod_template({"pod": ["x"]}, name="x")


# ── Extractor: no secrets (the second hard invariant) ───────────────────────────


def test_extract_never_copies_passphrases():
    net = {
        "networkId": "p",
        "pod": {
            "release": {"mode": "canary"},
            "admin_passphrase": "charles",
            "primary_passphrase": "darwin",
        },
        "bots": {"a": {"role": "primary"}},
    }
    t = extract_pod_template(net, name="p")
    text = t.to_yaml()
    assert "charles" not in text
    assert "darwin" not in text
    assert t.pod.release_mode == "canary"  # but the shape IS captured


def test_extract_does_not_copy_bot_token():
    net = {"bots": {"a": {"role": "primary", "user": "a", "token": "ghp_" + "z" * 36}}}
    t = extract_pod_template(net, name="p")
    assert "ghp_" not in t.to_yaml()


def test_extract_scrubs_secret_in_models_block():
    net = {
        "models": {"rungs": [{"name": "fast"}], "api_key": "sk-secret-aaaaaaaaaaaaaaaaaaaa"},
        "bots": {"a": {"role": "primary"}},
    }
    t = extract_pod_template(net, name="p")
    assert "api_key" not in t.to_yaml()
    assert "sk-secret" not in t.to_yaml()
    # Non-secret model shape survives.
    assert t.pod.model_tiers.get("rungs") == [{"name": "fast"}]


def test_extract_output_always_validates_clean():
    net = {
        "networkId": "home",
        "pod": {"release": {"mode": "canary"}, "admin_passphrase": "x"},
        "models": {"rungs": [{"name": "fast"}]},
        "podInvariantIntegrations": ["github", "brave"],
        "bots": {
            "evolve": {"role": "primary", "user": "evolve"},
            "member_bot": {"role": "member", "user": "member_bot", "channel": "telegram"},
        },
    }
    t = extract_pod_template(net, name="home")
    assert validate_pod_template(t).ok


# ── Extractor: pod-wide capture ─────────────────────────────────────────────────


def test_extract_pod_wide_block():
    net = {
        "networkId": "home",
        "pod": {"release": {"mode": "Canary"}},
        "models": {"roles": {"standard": "fast"}},
        "podInvariantIntegrations": ["github", "brave"],
        "bots": {"a": {"role": "primary"}},
    }
    t = extract_pod_template(net, name="home")
    assert t.pod.release_mode == "canary"  # normalized lower
    assert t.pod.model_tiers == {"roles": {"standard": "fast"}}
    assert t.pod.integrations == {
        "github": {"scope": "pod"},
        "brave": {"scope": "pod"},
    }


def test_extract_default_display_and_description():
    net = {"networkId": "home-pod", "bots": {"a": {"role": "primary"}}}
    t = extract_pod_template(net, name="captured")
    assert t.display_name == "home-pod"
    assert "home-pod" in t.description


# ── Extractor: per-bot + matching ───────────────────────────────────────────────


def test_extract_bot_no_match_keeps_apps():
    net = {"bots": {"a": {"role": "member", "user": "a"}}}
    ev = {"a": BotEvidence(installed_apps=("weird_unmatched_app",))}
    t = extract_pod_template(net, name="p", evidence=ev, candidates=[])
    bot = t.bots[0]
    assert bot.bot_template is None
    assert bot.installed_apps == ("weird_unmatched_app",)


def test_extract_bot_match_drops_apps():
    net = {"bots": {"a": {"role": "member", "user": "a"}}}
    ev = {"a": BotEvidence(installed_apps=("app_x",), installed_skills=("s1",))}
    cand = [_Tpl(name="tpl-x", applications=[_App(app_id="app_x")], skills=[_Skill(id="s1")])]
    t = extract_pod_template(net, name="p", evidence=ev, candidates=cand)
    bot = t.bots[0]
    assert bot.bot_template == "tpl-x"
    assert bot.installed_apps == ()  # redundant once a template matched


def test_extract_channel_role_from_network_and_evidence():
    net = {"bots": {"a": {"role": "primary", "user": "a"}}}
    ev = {"a": BotEvidence(channel="slack", voice_preset="warm")}
    t = extract_pod_template(net, name="p", evidence=ev, candidates=[])
    bot = t.bots[0]
    assert bot.role == "primary"  # network.json authoritative
    assert bot.channel == "slack"
    assert bot.voice_preset == "warm"


def test_extract_channel_from_network_entry():
    net = {"bots": {"a": {"role": "member", "user": "a", "channel": "telegram"}}}
    t = extract_pod_template(net, name="p")
    assert t.bots[0].channel == "telegram"


def test_extract_per_bot_integrations_from_evidence():
    net = {"bots": {"a": {"role": "primary", "user": "a"}}}
    ev = {"a": BotEvidence(overrides={"integrations": ["slack"]})}
    t = extract_pod_template(net, name="p", evidence=ev, candidates=[])
    assert t.pod.integrations.get("slack") == {"scope": "bot", "bots": ["a"]}
    # The per-bot integrations list is not duplicated into bot.overrides.
    assert "integrations" not in t.bots[0].overrides


# ── Matcher unit tests ──────────────────────────────────────────────────────────


def test_match_requires_app_overlap():
    ev = BotEvidence(installed_skills=("s1",))  # skills only, no apps
    cand = [_Tpl(name="t", applications=[_App(app_id="app_x")], skills=[_Skill(id="s1")])]
    assert match_bot_template(ev, cand) is None


def test_match_below_threshold_returns_none():
    # Template declares 1 app + 3 skills; bot has the app + 0 skills → 1/4 = 0.25.
    ev = BotEvidence(installed_apps=("app_x",))
    cand = [
        _Tpl(
            name="t",
            applications=[_App(app_id="app_x")],
            skills=[_Skill(id="s1"), _Skill(id="s2"), _Skill(id="s3")],
        )
    ]
    assert match_bot_template(ev, cand) is None


def test_match_by_app_name_not_just_id():
    ev = BotEvidence(installed_apps=("Morning Briefing",))
    cand = [_Tpl(name="mb", applications=[_App(app_id="app_mb", name="Morning Briefing")])]
    assert match_bot_template(ev, cand) == "mb"


def test_match_tie_break_prefers_more_matched_then_name():
    ev = BotEvidence(installed_apps=("app_x", "app_y"), installed_skills=("s1",))
    # Both fully covered (score 1.0); 'zeta' matches 3 units, 'alpha' matches 1.
    cand = [
        _Tpl(name="alpha", applications=[_App(app_id="app_x")]),
        _Tpl(
            name="zeta",
            applications=[_App(app_id="app_x"), _App(app_id="app_y")],
            skills=[_Skill(id="s1")],
        ),
    ]
    assert match_bot_template(ev, cand) == "zeta"


def test_match_no_candidates():
    assert match_bot_template(BotEvidence(installed_apps=("a",)), []) is None


def test_match_none_evidence():
    assert match_bot_template(None, [_Tpl(name="t")]) is None


# ── bot_evidence_from_files (pure FS-builder) ────────────────────────────────────


def test_bot_evidence_from_files_apps_and_channel():
    ev = bot_evidence_from_files(
        bot_cfg={"role": "member", "voice_preset": "warm"},
        oc_config={"channels": {"telegram": {}}, "plugins": ["gmail", "calendar"]},
        manifests=[{"id": "app_task_manager"}, {"pkg_id": "p-123"}, {"name": "Calendar Watch"}],
    )
    assert ev.role == "member"
    assert ev.voice_preset == "warm"
    assert ev.channel == "telegram"
    assert ev.installed_apps == ("app_task_manager", "p-123", "Calendar Watch")
    assert set(ev.installed_skills) == {"gmail", "calendar"}


def test_bot_evidence_channel_from_network_wins():
    ev = bot_evidence_from_files(
        bot_cfg={"channel": "slack"},
        oc_config={"channels": {"telegram": {}}},
    )
    assert ev.channel == "slack"


def test_bot_evidence_plugins_dict_shape():
    ev = bot_evidence_from_files(
        oc_config={"plugins": {"installed": {"gmail": {}, "weather": {}}}},
    )
    assert set(ev.installed_skills) == {"gmail", "weather"}


def test_bot_evidence_empty():
    ev = bot_evidence_from_files()
    assert ev.installed_apps == ()
    assert ev.channel is None


# ── write_pod_template ──────────────────────────────────────────────────────────


def test_write_pod_template_round_trips(tmp_path):
    net = {"networkId": "home", "bots": {"a": {"role": "primary", "user": "a"}}}
    t = extract_pod_template(net, name="home")
    dest = write_pod_template(t, templates_dir=tmp_path)
    assert dest == tmp_path / "home" / "pod.yaml"
    reloaded = load_pod_template("home", templates_dir=tmp_path)
    assert reloaded.to_payload() == t.to_payload()


def test_write_refuses_secret(tmp_path):
    # Hand-craft a template that smuggles a secret past the extractor by
    # building it from a dict directly, then confirm write refuses it.
    body = {
        "schema_version": 1,
        "name": "leaky",
        "pod": {"integrations": {"x": {"token": "ghp_" + "q" * 36}}},
        "bots": [{"bot_id": "a", "role": "primary"}],
    }
    t = pod_template_from_dict(body, name="leaky")
    with pytest.raises(PodTemplateExtractError):
        write_pod_template(t, templates_dir=tmp_path)


# ── Integration: extractor against the real gallery bot-templates ───────────────


def test_extract_matches_real_gallery_template():
    """A bot whose evidence mirrors the test-minimal template should match it.

    Uses the default candidate load (real gallery/bot-templates/), proving
    the extractor wires to the live bot-template loader.
    """
    net = {"networkId": "home", "bots": {"a": {"role": "primary", "user": "a"}}}
    ev = {"a": BotEvidence(installed_apps=("app_task_manager",), installed_skills=("messaging",))}
    t = extract_pod_template(net, name="home", evidence=ev)
    assert t.bots[0].bot_template == "test-minimal"
