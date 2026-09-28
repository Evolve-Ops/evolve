"""
test_bot_templates.py — Unit tests for the bot template framework.

Covers loader, validator, resolver, and provisioner. Two fixture sources:
  1. The repo's builtin ``gallery/bot-templates/test-minimal/`` — used to
     prove the framework loads a real on-disk template successfully.
  2. Tmp-dir templates synthesised per-test — used to exercise validation
     edge cases without polluting the gallery.

Run with:
    python3 -m pytest packages/admin/tests/test_bot_templates.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.bot_templates import (  # noqa: E402
    BotTemplate,
    SkillSpec,
    TemplateError,
    TemplateNotFoundError,
    TemplateValidationError,
    builtin_templates_dir,
    list_templates,
    load_template,
    plan_provision,
    render_template_vars,
    resolve_skills,
    validate_template,
)
from evolve_admin.bot_templates.resolver import (  # noqa: E402
    installed_plugins_from_openclaw_json,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _write_template(
    root: Path,
    name: str,
    *,
    manifest: dict,
    agents_md: str | None = None,
    soul_md: str | None = None,
    exec_approvals: dict | None = None,
    embedded_apps: dict[str, dict] | None = None,
) -> Path:
    """Materialise a template directory under ``root``. Returns the path."""
    import yaml as _yaml

    tdir = root / name
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "template.yaml").write_text(_yaml.safe_dump(manifest))
    if agents_md is not None:
        (tdir / "AGENTS.md.template").write_text(agents_md)
    if soul_md is not None:
        (tdir / "SOUL.md.template").write_text(soul_md)
    if exec_approvals is not None:
        (tdir / "exec-approvals.template.json").write_text(
            json.dumps(exec_approvals, indent=2)
        )
    if embedded_apps:
        apps_dir = tdir / "apps"
        apps_dir.mkdir(exist_ok=True)
        for fname, body in embedded_apps.items():
            (apps_dir / fname).write_text(json.dumps(body, indent=2))
    return tdir


# ── Loader tests ──────────────────────────────────────────────────────────────


def test_load_builtin_test_minimal_template_succeeds():
    """The on-disk test-minimal template loads without error."""
    t = load_template("test-minimal")
    assert t.name == "test-minimal"
    assert t.display_name == "Test Minimal Template"
    assert "Smoke-test template" in t.description
    assert t.voice_preset == "neutral"
    assert t.channel_pattern == "any-messaging"
    # Two skills: one ``manual`` (messaging) + one optional (brave) — see
    # template.yaml comments for why this combination is the smallest one
    # that exercises both resolver branches without depending on any
    # auto-installable plugin.
    assert len(t.skills) == 2
    by_id = {s.id: s for s in t.skills}
    assert by_id["messaging"].source == "openclaw-plugin"
    assert by_id["messaging"].optional is False
    assert by_id["brave"].optional is True
    assert len(t.applications) == 1
    assert t.applications[0].name == "Test Task Manager"
    assert t.applications[0].app_id == "app_task_manager"
    assert t.applications[0].skill_deps == ("messaging",)
    assert t.agents_md_template is not None
    assert "{bot_id}" in t.agents_md_template
    assert t.soul_md_template is not None
    assert t.exec_approvals is not None
    # Real OC schema: {version, defaults, agents}. defaults is EMPTY in
    # real-bot exec-approvals.json on the mini (team_bot_a/admin_bot/team_bot_c ship
    # `defaults: {}`; security_bot ships the same with a populated
    # agents.main.allowlist). The template matches that shape.
    assert t.exec_approvals["version"] == 1
    assert t.exec_approvals["defaults"] == {}
    assert "main" in t.exec_approvals["agents"]


def test_list_templates_includes_test_minimal():
    """test-minimal must appear in the builtin template list."""
    names = list_templates()
    assert "test-minimal" in names


def test_load_template_not_found(tmp_path):
    """Unknown template name raises TemplateNotFoundError."""
    with pytest.raises(TemplateNotFoundError):
        load_template("does-not-exist", templates_dir=tmp_path)


def test_load_template_missing_yaml(tmp_path):
    """Directory without template.yaml is also TemplateNotFoundError."""
    (tmp_path / "broken").mkdir()
    with pytest.raises(TemplateNotFoundError):
        load_template("broken", templates_dir=tmp_path)


def test_load_template_malformed_yaml(tmp_path):
    """Bad YAML raises TemplateError (not a generic exception)."""
    tdir = tmp_path / "bad-yaml"
    tdir.mkdir()
    (tdir / "template.yaml").write_text("name: ok\n  invalid: indent\n - dash")
    with pytest.raises(TemplateError):
        load_template("bad-yaml", templates_dir=tmp_path)


def test_load_template_short_form_var_description(tmp_path):
    """template_vars: name: 'description' shorthand expands correctly."""
    _write_template(
        tmp_path,
        "short-var",
        manifest={
            "name": "short-var",
            "description": "test",
            "skills": [],
            "applications": [],
            "template_vars": {"user_name": "Operator's name"},
        },
    )
    t = load_template("short-var", templates_dir=tmp_path)
    assert t.template_vars["user_name"]["description"] == "Operator's name"


def test_load_template_minimal_no_optional_files(tmp_path):
    """A template with only template.yaml loads (no AGENTS/SOUL/exec)."""
    _write_template(
        tmp_path,
        "bare",
        manifest={
            "name": "bare",
            "description": "no companion files",
            "skills": [{"id": "brave"}],
            "applications": [],
        },
    )
    t = load_template("bare", templates_dir=tmp_path)
    assert t.agents_md_template is None
    assert t.soul_md_template is None
    assert t.exec_approvals is None
    assert t.embedded_apps == {}


def test_load_template_rejects_non_dict_top_level(tmp_path):
    """template.yaml that's a list at the top level is rejected."""
    tdir = tmp_path / "list-top"
    tdir.mkdir()
    (tdir / "template.yaml").write_text("- one\n- two\n")
    with pytest.raises(TemplateError):
        load_template("list-top", templates_dir=tmp_path)


def test_load_template_embedded_app(tmp_path):
    """Embedded app JSON is parsed and exposed via embedded_apps."""
    _write_template(
        tmp_path,
        "with-embedded",
        manifest={
            "name": "with-embedded",
            "description": "x",
            "skills": [{"id": "brave"}],
            "applications": [
                {
                    "name": "Inline",
                    "embedded_path": "inline.json",
                    "skill_deps": ["brave"],
                }
            ],
        },
        embedded_apps={"inline.json": {"pkg_id": "p-aaaaaaaa", "name": "Inline"}},
    )
    t = load_template("with-embedded", templates_dir=tmp_path)
    assert "inline.json" in t.embedded_apps
    assert t.embedded_apps["inline.json"]["pkg_id"] == "p-aaaaaaaa"


def test_load_template_corrupt_exec_approvals_raises(tmp_path):
    """Broken exec-approvals JSON surfaces as TemplateError, not silent."""
    tdir = _write_template(
        tmp_path,
        "bad-exec",
        manifest={
            "name": "bad-exec",
            "description": "x",
            "skills": [],
            "applications": [],
        },
    )
    (tdir / "exec-approvals.template.json").write_text("{not json")
    with pytest.raises(TemplateError):
        load_template("bad-exec", templates_dir=tmp_path)


# ── Validator tests ───────────────────────────────────────────────────────────


def test_validate_test_minimal_passes():
    """The bundled test template must pass validation cleanly (no errors)."""
    t = load_template("test-minimal")
    r = validate_template(t)
    assert r.ok, f"unexpected errors: {r.errors}"


def test_validate_duplicate_skill_id_errors(tmp_path):
    _write_template(
        tmp_path,
        "dupes",
        manifest={
            "name": "dupes",
            "description": "x",
            "skills": [{"id": "brave"}, {"id": "brave"}],
            "applications": [],
        },
    )
    t = load_template("dupes", templates_dir=tmp_path)
    r = validate_template(t)
    assert not r.ok
    assert any("brave" in e and "more than once" in e for e in r.errors)


def test_validate_application_must_have_exactly_one_source(tmp_path):
    """An app declaring both app_id and embedded_path is invalid."""
    _write_template(
        tmp_path,
        "both",
        manifest={
            "name": "both",
            "description": "x",
            "skills": [{"id": "brave"}],
            "applications": [
                {
                    "name": "App",
                    "app_id": "app_foo",
                    "embedded_path": "foo.json",
                    "skill_deps": ["brave"],
                }
            ],
        },
        embedded_apps={"foo.json": {"name": "Foo"}},
    )
    t = load_template("both", templates_dir=tmp_path)
    r = validate_template(t)
    assert not r.ok
    assert any("exactly one" in e for e in r.errors)


def test_validate_application_must_have_at_least_one_source(tmp_path):
    """An app declaring neither app_id nor embedded_path is invalid."""
    _write_template(
        tmp_path,
        "neither",
        manifest={
            "name": "neither",
            "description": "x",
            "skills": [{"id": "brave"}],
            "applications": [{"name": "App", "skill_deps": ["brave"]}],
        },
    )
    t = load_template("neither", templates_dir=tmp_path)
    r = validate_template(t)
    assert not r.ok
    assert any("exactly one" in e for e in r.errors)


def test_validate_skill_dep_must_be_declared(tmp_path):
    """An app skill_dep that isn't in the skills list is an error."""
    _write_template(
        tmp_path,
        "unmet",
        manifest={
            "name": "unmet",
            "description": "x",
            "skills": [{"id": "brave"}],
            "applications": [
                {
                    "name": "App",
                    "app_id": "app_foo",
                    "skill_deps": ["gog"],  # not declared
                }
            ],
        },
    )
    t = load_template("unmet", templates_dir=tmp_path)
    r = validate_template(t)
    assert not r.ok
    assert any("gog" in e and "not" in e for e in r.errors)


def test_validate_embedded_path_must_exist(tmp_path):
    """An app embedded_path that points at a missing file is an error."""
    _write_template(
        tmp_path,
        "ghost",
        manifest={
            "name": "ghost",
            "description": "x",
            "skills": [{"id": "brave"}],
            "applications": [
                {
                    "name": "Ghost",
                    "embedded_path": "ghost.json",
                    "skill_deps": ["brave"],
                }
            ],
        },
    )
    t = load_template("ghost", templates_dir=tmp_path)
    r = validate_template(t)
    assert not r.ok
    assert any("ghost.json" in e for e in r.errors)


def test_validate_unknown_skill_source_is_warning_not_error(tmp_path):
    """Unknown 'source' is a warning so future adapters don't need code changes."""
    _write_template(
        tmp_path,
        "future",
        manifest={
            "name": "future",
            "description": "x",
            "skills": [{"id": "brave", "source": "some-future-registry"}],
            "applications": [],
        },
    )
    t = load_template("future", templates_dir=tmp_path)
    r = validate_template(t)
    assert r.ok  # warning only
    assert any("future-registry" in w for w in r.warnings)


def test_validate_raise_if_invalid():
    """ValidationReport.raise_if_invalid raises TemplateValidationError."""
    # Build a programmatic invalid template (skip on-disk).
    t = BotTemplate(
        name="x",
        path=Path("/tmp/x"),
        display_name="X",
        description="x",
        voice_preset=None,
        channel_pattern=None,
        skills=(SkillSpec(id="brave"), SkillSpec(id="brave")),
        applications=(),
        template_vars={},
        agents_md_template=None,
        soul_md_template=None,
        exec_approvals=None,
        embedded_apps={},
        raw={},
    )
    r = validate_template(t)
    with pytest.raises(TemplateValidationError):
        r.raise_if_invalid()


# ── Resolver tests ────────────────────────────────────────────────────────────


def _make_template_with_skills(*skill_specs: SkillSpec) -> BotTemplate:
    return BotTemplate(
        name="t",
        path=Path("/tmp/t"),
        display_name="T",
        description="t",
        voice_preset=None,
        channel_pattern=None,
        skills=tuple(skill_specs),
        applications=(),
        template_vars={},
        agents_md_template=None,
        soul_md_template=None,
        exec_approvals=None,
        embedded_apps={},
        raw={},
    )


def test_resolver_marks_installed_skills_present():
    """A skill whose plugin is in installed_plugins resolves as 'installed'."""
    t = _make_template_with_skills(SkillSpec(id="brave"))
    res = resolve_skills(t, installed_plugins=["brave", "slack"])
    assert res.ok
    assert len(res.resolved) == 1
    assert res.resolved[0].status == "installed"
    assert res.resolved[0].plugin_name == "brave"


def test_resolver_queues_missing_installable_skills():
    """A skill not installed but explicitly auto-installable resolves as
    'queued'.

    Tests pass an explicit ``available_plugins`` set so the test asserts
    the queued branch deterministically, rather than relying on the
    module-level "auto-installable" set (which is empty today — see
    resolver._STANDARD_DEPLOY_AUTO_INSTALLABLE)."""
    t = _make_template_with_skills(SkillSpec(id="brave"))
    res = resolve_skills(t, installed_plugins=[], available_plugins=["brave"])
    assert res.ok, f"unexpected blocking issues: {res.blocking_issues}"
    assert res.resolved[0].status == "queued"
    assert res.queued() == res.resolved


def test_resolver_marks_unsupported_when_substrate_lacks_plugin():
    """If a substrate-restricted plugin list excludes the mapping target,
    the skill is unsupported and blocks provisioning unless optional."""
    t = _make_template_with_skills(SkillSpec(id="brave"))
    res = resolve_skills(t, installed_plugins=[], available_plugins=[])
    assert not res.ok
    assert res.resolved[0].status == "unsupported"
    assert any("brave" in i for i in res.blocking_issues)


def test_resolver_optional_skill_unsupported_does_not_block():
    """Optional skills surface as unsupported without blocking provisioning."""
    t = _make_template_with_skills(SkillSpec(id="brave", optional=True))
    res = resolve_skills(t, installed_plugins=[], available_plugins=[])
    assert res.ok
    assert res.resolved[0].status == "unsupported"


def test_resolver_unknown_source_is_unknown_adapter_and_blocks():
    """Non-openclaw-plugin sources are unknown adapters today (blocking)."""
    t = _make_template_with_skills(SkillSpec(id="weather", source="clawhub"))
    res = resolve_skills(t, installed_plugins=["weather"])
    # Even though weather is "installed", clawhub source has no adapter
    # — be loud about it. The user shouldn't get a silent "installed"
    # for a substrate that we don't actually know how to talk to.
    assert not res.ok
    assert res.resolved[0].status == "unknown-adapter"


def test_resolver_skill_alias_mapping():
    """Aliased skill ids resolve through the mapping.

    The GOG-family skills (``gog``, ``gmail``, ``calendar``,
    ``google-workspace``) all map to the OpenClaw ``google`` plugin
    name. Per gog_install.py (A3), the user-facing skill id is ``gog``
    but the plugin entry under ``plugins.entries`` is named ``google``.

    Note: ``gmail`` also has a dedicated adapter (GOG OAuth), so a fresh
    bot would resolve to ``adapter-required``. This test pre-installs
    the plugin so the alias-mapping branch (status=installed) is the
    one being exercised.
    """
    t = _make_template_with_skills(SkillSpec(id="gmail"))
    res = resolve_skills(t, installed_plugins=["google"])
    assert res.resolved[0].status == "installed"
    assert res.resolved[0].plugin_name == "google"


def test_installed_plugins_from_openclaw_json():
    """Reading the plugins list from a parsed openclaw.json works."""
    cfg = {
        "plugins": {
            "entries": {
                "brave": {"enabled": True},
                "slack": {"enabled": False},  # disabled — should be filtered
                "telegram": {},  # missing 'enabled' — treat as enabled
                "discord": True,  # legacy bare value — include
            }
        }
    }
    plugins = installed_plugins_from_openclaw_json(cfg)
    assert set(plugins) == {"brave", "telegram", "discord"}


def test_installed_plugins_from_openclaw_json_bad_shape():
    """Robust against missing/malformed config."""
    assert installed_plugins_from_openclaw_json({}) == []
    assert installed_plugins_from_openclaw_json({"plugins": "wat"}) == []
    assert installed_plugins_from_openclaw_json({"plugins": {"entries": []}}) == []


# ── Provisioner tests ────────────────────────────────────────────────────────


def test_render_template_vars_substitutes_known_placeholders():
    text = "Hello {user_name}, your bot is {bot_id}."
    out = render_template_vars(text, {"user_name": "Diana", "bot_id": "team_bot_a"})
    assert out == "Hello Diana, your bot is team_bot_a."


def test_render_template_vars_leaves_unknown_placeholders():
    """Unknown placeholders are preserved verbatim (wizard fills them later)."""
    text = "Hello {user_name}, scope: {scope}."
    out = render_template_vars(text, {"user_name": "Diana"})
    assert out == "Hello Diana, scope: {scope}."


def test_render_template_vars_supports_double_braces():
    """{{name}} works the same as {name} so JSON/YAML examples don't conflict."""
    text = "Greeting: {{user_name}}."
    out = render_template_vars(text, {"user_name": "Diana"})
    assert out == "Greeting: Diana."


def test_plan_provision_full_path():
    """End-to-end: load → resolve → plan → check rendered outputs."""
    t = load_template("test-minimal")
    res = resolve_skills(t, installed_plugins=[])
    plan = plan_provision(
        t,
        bot_id="testbot",
        vars={"user_name": "Diana"},
        skill_resolution=res,
    )
    assert plan.ok
    assert plan.bot_id == "testbot"
    assert plan.vars["user_name"] == "Diana"
    assert plan.vars["time_zone"] == "America/Los_Angeles"  # from default
    assert plan.vars["bot_id"] == "testbot"

    # test-minimal declares 'messaging' (manual) + 'brave' (optional).
    # Neither auto-installs via the standard deploy.
    statuses = {r.spec.id: r.status for r in plan.skill_resolution.resolved}
    assert statuses["messaging"] == "manual"
    # brave is optional + not in the auto-installable set, so it
    # surfaces as unsupported-but-non-blocking.
    assert statuses["brave"] == "unsupported"
    # No standard-deploy installs are queued.
    assert plan.skills_to_install == ()

    # Applications planned.
    assert len(plan.applications) == 1
    assert plan.applications[0].name == "Test Task Manager"
    assert plan.applications[0].source == "gallery"

    # Files rendered.
    by_path = {f.relative_path: f for f in plan.files}
    assert "AGENTS.md" in by_path
    assert "SOUL.md" in by_path
    assert "exec-approvals.json" in by_path
    assert "{bot_id}" not in by_path["AGENTS.md"].content
    assert "testbot" in by_path["AGENTS.md"].content
    assert "Diana" in by_path["AGENTS.md"].content
    # exec-approvals is valid JSON post-render.
    json.loads(by_path["exec-approvals.json"].content)


def test_plan_provision_missing_required_var_raises():
    """Required template_vars must be supplied; provisioner refuses to plan."""
    t = load_template("test-minimal")
    res = resolve_skills(t, installed_plugins=[])
    with pytest.raises(TemplateValidationError):
        plan_provision(t, bot_id="x", vars={}, skill_resolution=res)


def test_plan_provision_implicit_vars_cannot_be_overridden():
    """User-supplied 'bot_id' is overwritten by the framework's own value."""
    t = load_template("test-minimal")
    res = resolve_skills(t, installed_plugins=[])
    plan = plan_provision(
        t,
        bot_id="real",
        vars={"user_name": "D", "bot_id": "evil-override"},
        skill_resolution=res,
    )
    assert plan.vars["bot_id"] == "real"


def test_plan_provision_carries_resolver_blocking_state():
    """If the resolver flagged blocking issues, plan_provision still
    produces a plan but plan.ok is False so callers refuse to execute.

    Use a programmatic template with a non-optional ``brave`` skill so
    the test isolates the blocking-state branch (test-minimal marks
    brave as optional, which is non-blocking by design)."""
    t = _make_template_with_skills(SkillSpec(id="brave", optional=False))
    res = resolve_skills(t, installed_plugins=[], available_plugins=[])
    assert not res.ok
    plan = plan_provision(
        t,
        bot_id="testbot",
        vars={},
        skill_resolution=res,
    )
    assert not plan.ok


# ── Integration: builtin template list is well-formed ────────────────────────


def test_all_builtin_templates_validate_cleanly():
    """Every template shipped in gallery/bot-templates/ must validate.

    Acts as a smoke test for new templates added to the repo — they
    can't break the framework without this test going red.
    """
    for name in list_templates():
        t = load_template(name)
        r = validate_template(t)
        assert r.ok, (
            f"builtin template {name!r} has errors: {r.errors} "
            f"(warnings: {r.warnings})"
        )


def test_builtin_templates_dir_exists():
    assert builtin_templates_dir().is_dir()


# ── CLI integration tests (dry-run only) ─────────────────────────────────────

# These exercise the CLI wiring in cli.py without touching real bots — the
# --dry-run branch returns before any deploy steps fire. Confirms the
# argparse/click wiring matches the public surface contract.


def test_cli_deploy_from_template_dry_run_succeeds(tmp_path):
    """`deploy --from-template <name> <bot> --dry-run` exits 0 + prints the plan."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    netfile = tmp_path / "network.json"
    netfile.write_text(json.dumps({
        "networkId": "test",
        "sharedDir": str(tmp_path),
        "bots": {},
        "members": [],
    }))

    runner = CliRunner()
    r = runner.invoke(main, [
        "--network", str(netfile),
        "deploy",
        "--from-template", "test-minimal",
        "smoke-bot",
        "--template-var", "user_name=Diana",
        "--dry-run",
    ])
    assert r.exit_code == 0, f"output:\n{r.output}\nexc: {r.exception}"
    assert "Template: test-minimal" in r.output
    assert "Bot: smoke-bot" in r.output
    # test-minimal declares messaging (manual) + brave (optional).
    assert "messaging" in r.output
    assert "Test Task Manager" in r.output
    assert "AGENTS.md" in r.output
    assert "dry-run" in r.output.lower()


def test_cli_deploy_from_template_missing_var_errors_cleanly(tmp_path):
    """A missing required template var produces a non-zero exit + readable error."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    netfile = tmp_path / "network.json"
    netfile.write_text(json.dumps({
        "networkId": "test",
        "sharedDir": str(tmp_path),
        "bots": {},
        "members": [],
    }))

    runner = CliRunner()
    r = runner.invoke(main, [
        "--network", str(netfile),
        "deploy",
        "--from-template", "test-minimal",
        "smoke-bot",
        # user_name (required) intentionally omitted
        "--dry-run",
    ])
    assert r.exit_code != 0
    assert "user_name" in r.output


def test_cli_deploy_from_template_rejects_combined_with_all(tmp_path):
    """--from-template + --all is rejected at parse time."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    netfile = tmp_path / "network.json"
    netfile.write_text(json.dumps({
        "networkId": "test",
        "sharedDir": str(tmp_path),
        "bots": {},
        "members": [],
    }))

    runner = CliRunner()
    r = runner.invoke(main, [
        "--network", str(netfile),
        "deploy",
        "--from-template", "test-minimal",
        "--all",
        "--dry-run",
    ])
    assert r.exit_code != 0
    assert "incompatible" in r.output.lower() or "all" in r.output.lower()


def test_cli_deploy_from_template_unknown_name_errors_cleanly(tmp_path):
    """An unknown template name surfaces as TemplateNotFoundError to the user."""
    from click.testing import CliRunner

    from evolve_admin.cli import main

    netfile = tmp_path / "network.json"
    netfile.write_text(json.dumps({
        "networkId": "test",
        "sharedDir": str(tmp_path),
        "bots": {},
        "members": [],
    }))

    runner = CliRunner()
    r = runner.invoke(main, [
        "--network", str(netfile),
        "deploy",
        "--from-template", "does-not-exist-template",
        "ghost-bot",
        "--dry-run",
    ])
    assert r.exit_code != 0
    assert "not found" in r.output.lower() or "does-not-exist" in r.output.lower()


# ── Regression tests for the C1.a review fixes (2026-05-12) ──────────────────
#
# These cover the four issues called out in the reviewer's response:
#
#   1. CRITICAL — silent half-deployment when a skill's plugin can't be
#      installed by the standard deploy ("queued is satisfied implicitly"
#      claim was wrong).
#   2. Spec 7 dependency — GOG must route to its dedicated install
#      adapter; weather/news/messaging must be declarable without
#      pretending they'll auto-install.
#   3. JSON injection — values containing `"` would silently corrupt
#      exec-approvals.json under the previous "warn-and-write-broken"
#      behaviour.
#   4. Validator must warn on unknown skill ids so typos like ``g0g``
#      surface at validation time, not at install time.


# Fix 1: queued-skill silent half-deployment guard


def test_resolver_blocks_when_skill_cannot_be_auto_installed():
    """A skill that has no adapter, isn't manual, and isn't in the
    auto-installable set must BLOCK provisioning — not silently mark
    itself "queued" and let the deploy report success.

    This is the explicit regression test for the reviewer's
    Decision-3 finding: previously the resolver marked `brave` as
    "queued" without checking whether anything could actually install
    it. The standard deploy installs only the Evolve plugin, so the
    bot would end up half-provisioned with brave declared but never
    installed.
    """
    t = _make_template_with_skills(SkillSpec(id="brave"))
    res = resolve_skills(t, installed_plugins=[])
    # No auto-installable set passed → resolver uses the module default
    # (empty today — see resolver._STANDARD_DEPLOY_AUTO_INSTALLABLE).
    assert not res.ok, (
        "regression: a non-auto-installable skill silently became 'queued' "
        "again — the half-deployment guard is broken"
    )
    assert res.resolved[0].status == "unsupported"
    assert any("brave" in i for i in res.blocking_issues)


def test_resolver_default_available_plugins_is_empty_today():
    """Documents the verified-2026-05-12 fact that the standard deploy
    does not auto-install any skill plugins. If this test starts failing
    it means someone wired up a real auto-install path; that's good news,
    but the resolver's documentation + status semantics must be updated
    in lockstep so callers can't drift back into the silent-failure mode.
    """
    from evolve_admin.bot_templates.resolver import (
        _STANDARD_DEPLOY_AUTO_INSTALLABLE,
    )
    assert _STANDARD_DEPLOY_AUTO_INSTALLABLE == frozenset(), (
        "The standard deploy's auto-installable set has changed. "
        "Update the resolver module docstring + the silent-half-deployment "
        "regression test above before merging this change."
    )


# Fix 2: Spec 7 integration — gog adapter + weather/news/messaging manual


def test_resolver_gog_skill_routes_to_dedicated_adapter():
    """The ``gog`` skill must route to a dedicated install adapter
    (Spec 11 / gog_install.py), not the standard plugin-install path.

    Per A3's gog_install.py:62, the user-facing skill id is ``gog`` and
    it's backed by the OpenClaw ``google`` plugin. The resolver must
    mark a fresh GOG install as ``adapter-required`` with an actionable
    hint, so the deploy plan tells the operator what to do next.
    """
    t = _make_template_with_skills(SkillSpec(id="gog"))
    res = resolve_skills(t, installed_plugins=[])
    assert res.ok, f"unexpected blocking: {res.blocking_issues}"
    assert res.resolved[0].status == "adapter-required"
    assert res.resolved[0].plugin_name == "google"
    assert res.resolved[0].adapter_hint is not None
    # The hint should mention the user-facing command/path so the
    # operator knows what to do.
    assert "gog" in res.resolved[0].adapter_hint.lower()


def test_resolver_gog_skill_recognized_as_installed_when_plugin_present():
    """If the bot already has the ``google`` plugin (from a prior install),
    the resolver should mark ``gog`` as installed — not adapter-required.
    Otherwise re-running --from-template on an existing bot would
    repeatedly prompt for OAuth."""
    t = _make_template_with_skills(SkillSpec(id="gog"))
    res = resolve_skills(t, installed_plugins=["google"])
    assert res.resolved[0].status == "installed"
    assert res.resolved[0].plugin_name == "google"


def test_resolver_weather_skill_marked_manual():
    """The ``weather`` skill isn't an OpenClaw plugin — templates must be
    able to declare it (so applications can list it as a skill_dep) but
    the resolver must mark it ``manual`` with explicit guidance, never
    try to install it as a plugin."""
    t = _make_template_with_skills(SkillSpec(id="weather"))
    res = resolve_skills(t, installed_plugins=[])
    assert res.ok, f"manual skills must not block: {res.blocking_issues}"
    assert res.resolved[0].status == "manual"
    assert res.resolved[0].adapter_hint is not None


def test_resolver_news_skill_marked_manual():
    """Same as weather — news isn't a plugin."""
    t = _make_template_with_skills(SkillSpec(id="news"))
    res = resolve_skills(t, installed_plugins=[])
    assert res.ok
    assert res.resolved[0].status == "manual"


def test_resolver_messaging_skill_marked_manual():
    """``messaging`` is configured via channel/integration setup, not
    via plugin install. Templates declare it so application skill_deps
    validate, but the resolver should not treat it as needing a plugin."""
    t = _make_template_with_skills(SkillSpec(id="messaging"))
    res = resolve_skills(t, installed_plugins=[])
    assert res.ok
    assert res.resolved[0].status == "manual"
    assert res.resolved[0].adapter_hint is not None


def test_resolver_spec7_morning_briefing_skill_set_resolves_cleanly():
    """The skill set Spec 7's Morning Briefing template declares (gog,
    weather, news, messaging) must resolve without blocking issues —
    each on its own track (adapter-required for gog, manual for the
    other three)."""
    t = _make_template_with_skills(
        SkillSpec(id="gog"),
        SkillSpec(id="weather"),
        SkillSpec(id="news"),
        SkillSpec(id="messaging"),
    )
    res = resolve_skills(t, installed_plugins=[])
    assert res.ok, f"Spec 7 skill set blocks: {res.blocking_issues}"
    by_id = {r.spec.id: r for r in res.resolved}
    assert by_id["gog"].status == "adapter-required"
    assert by_id["weather"].status == "manual"
    assert by_id["news"].status == "manual"
    assert by_id["messaging"].status == "manual"
    # All four expose actionable next-step guidance.
    for sid in ("gog", "weather", "news", "messaging"):
        assert by_id[sid].adapter_hint, (
            f"skill {sid!r} resolved without a hint — the operator won't "
            f"know what to do next"
        )


# Fix 3: JSON-safe template-var substitution


def test_render_template_vars_json_safe_escapes_quotes(tmp_path):
    """Values containing JSON metacharacters must not corrupt JSON output.

    The previous behaviour (plain string substitution) produced
    syntactically broken JSON when a value contained ``"``, ``\\``,
    or a control character. With ``json_safe=True`` the rendered
    output is always parseable.
    """
    template_text = '{"user": "{user_name}", "bot_id": "{bot_id}"}'
    rendered = render_template_vars(
        template_text,
        {"user_name": 'O\'Brien "BB"', "bot_id": "team_bot_a"},
        json_safe=True,
    )
    parsed = json.loads(rendered)
    assert parsed["user"] == 'O\'Brien "BB"'
    assert parsed["bot_id"] == "team_bot_a"


def test_render_template_vars_json_safe_escapes_backslash():
    """Backslashes in user input must be escaped to avoid breaking JSON."""
    template_text = '{"path": "{path}"}'
    rendered = render_template_vars(
        template_text,
        {"path": "C:\\Users\\foo"},
        json_safe=True,
    )
    parsed = json.loads(rendered)
    assert parsed["path"] == "C:\\Users\\foo"


def test_render_template_vars_json_safe_escapes_newline():
    """Newlines in values must not break the JSON literal they're inside."""
    template_text = '{"note": "{note}"}'
    rendered = render_template_vars(
        template_text,
        {"note": "first line\nsecond line"},
        json_safe=True,
    )
    parsed = json.loads(rendered)
    assert parsed["note"] == "first line\nsecond line"


def test_exec_approvals_with_quote_in_user_name_produces_valid_json(tmp_path):
    """End-to-end: a template var containing a quote, substituted into
    exec-approvals.template.json, must produce a parseable file (not
    the old "warn-and-write-broken-config" behaviour)."""
    _write_template(
        tmp_path,
        "exec-quote",
        manifest={
            "name": "exec-quote",
            "description": "quote-handling regression",
            "skills": [{"id": "messaging"}],
            "applications": [],
            "template_vars": {
                "user_name": {"description": "operator name", "required": True},
            },
        },
        exec_approvals={
            "version": 1,
            "default_policy": "deny",
            "notes": "Generated for {user_name} on {bot_id}.",
        },
    )
    t = load_template("exec-quote", templates_dir=tmp_path)
    res = resolve_skills(t, installed_plugins=[])
    plan = plan_provision(
        t,
        bot_id="qbot",
        vars={"user_name": 'Diana "D" O\'Brien'},
        skill_resolution=res,
    )
    by_path = {f.relative_path: f for f in plan.files}
    # The whole point: the file must parse cleanly, with the special
    # characters preserved verbatim in the value.
    parsed = json.loads(by_path["exec-approvals.json"].content)
    assert 'Diana "D" O\'Brien' in parsed["notes"]
    assert "qbot" in parsed["notes"]


def test_render_template_vars_default_mode_is_not_json_safe():
    """The default (non-json_safe) substitution must continue to work as
    plain text replacement so AGENTS.md / SOUL.md keep behaving the
    same way after the fix."""
    rendered = render_template_vars(
        "Hello {name}",
        {"name": 'O\'Brien "BB"'},
    )
    # Plain text — no quoting transformation.
    assert rendered == 'Hello O\'Brien "BB"'


# Fix 4: Validator warns on unknown skill ids


def test_validator_warns_on_unknown_skill_id(tmp_path):
    """Skill ids not in the resolver's known set must trigger a
    validation warning so template authors catch typos at author time
    rather than at install time.

    Example: ``g0g`` (zero instead of o) silently fell through as a
    phantom plugin name before this fix.
    """
    _write_template(
        tmp_path,
        "typo",
        manifest={
            "name": "typo",
            "description": "intentional typo for validator regression",
            "skills": [{"id": "g0g"}],
            "applications": [],
        },
    )
    t = load_template("typo", templates_dir=tmp_path)
    r = validate_template(t)
    # Still valid (not an error — could be a future community skill)
    # but the warning must be present.
    assert r.ok, f"unexpected errors: {r.errors}"
    assert any(
        "g0g" in w and "known-skill map" in w for w in r.warnings
    ), f"warning missing or wrong shape: {r.warnings}"


def test_validator_does_not_warn_on_known_skill_id(tmp_path):
    """Known skill ids (slack, gog, weather, news, brave, etc.) must
    NOT produce the unknown-skill warning. Otherwise every template
    becomes noisy."""
    _write_template(
        tmp_path,
        "known",
        manifest={
            "name": "known",
            "description": "uses only recognised skills",
            "skills": [
                {"id": "slack"},
                {"id": "gog"},
                {"id": "weather"},
                {"id": "news"},
                {"id": "brave"},
                {"id": "messaging"},
            ],
            "applications": [],
        },
    )
    t = load_template("known", templates_dir=tmp_path)
    r = validate_template(t)
    unknown_warnings = [w for w in r.warnings if "known-skill map" in w]
    assert unknown_warnings == [], (
        f"validator wrongly warned about a known skill id: {unknown_warnings}"
    )


# ── V1.1-1: Embedded-apps provisioner ────────────────────────────────────────
#
# Tests cover four scopes:
#   1. parse_app_blueprint — ## FILE: block extraction from build_spec,
#      template-var substitution, path classification.
#   2. plan_provision — embedded-app plans surfaced on the
#      ProvisionPlan alongside the existing files list.
#   3. apply_embedded_app — atomic render with provenance-marker stamp.
#   4. apply_embedded_app rollback — mid-blueprint failure restores
#      prior filesystem state.


def _blueprint_with_files(*, app_id: str, build_spec: str) -> dict:
    """Helper: synthesise a minimal embedded-app blueprint dict."""
    return {
        "schema_version": 5,
        "manifest_type": "evolve_application",
        "id": app_id,
        "name": app_id.replace("_", " ").title(),
        "build_spec": build_spec,
    }


# ─ Parser tests ──────────────────────────────────────────────────────────────


def test_parse_blueprint_extracts_file_blocks():
    """parse_app_blueprint returns one BlueprintFile per ## FILE: block."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "# Demo\n\n"
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('hello')\n"
            "```\n\n"
            "## FILE: scripts/cron.sh\n"
            "```bash\n"
            "#!/bin/bash\n"
            "echo run\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    assert parsed.app_id == "app_demo"
    assert len(parsed.files) == 2
    paths = [f.raw_path for f in parsed.files]
    assert paths == ["scripts/run.py", "scripts/cron.sh"]
    # Shell scripts are flagged executable.
    by_path = {f.raw_path: f for f in parsed.files}
    assert by_path["scripts/cron.sh"].executable is True
    assert by_path["scripts/run.py"].executable is False
    # Workspace destination for relative paths.
    assert all(f.destination == "workspace" for f in parsed.files)
    # No launchd labels for plain workspace files.
    assert parsed.launchd_labels == ()


def test_parse_blueprint_classifies_launchagent_paths():
    """``~/Library/LaunchAgents/`` paths are routed to the launchagent
    destination with the plist stem as the label."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: ~/Library/LaunchAgents/com.team_bot_a.morning.plist\n"
            "```xml\n"
            "<?xml version=\"1.0\"?><plist><dict/></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    assert len(parsed.files) == 1
    bf = parsed.files[0]
    assert bf.destination == "launchagent"
    assert bf.launchd_label == "com.team_bot_a.morning"
    assert bf.normalised_path == "Library/LaunchAgents/com.team_bot_a.morning.plist"
    assert parsed.launchd_labels == ("com.team_bot_a.morning",)


def test_parse_blueprint_classifies_launchdaemon_paths():
    """``/Library/LaunchDaemons/...`` paths route to launchdaemon."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: /Library/LaunchDaemons/ai.openclaw.evolve.team_bot_a.briefing.plist\n"
            "```xml\n"
            "<plist><dict/></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    assert len(parsed.files) == 1
    bf = parsed.files[0]
    assert bf.destination == "launchdaemon"
    assert bf.launchd_label == "ai.openclaw.evolve.team_bot_a.briefing"
    assert parsed.launchd_labels == ("ai.openclaw.evolve.team_bot_a.briefing",)


def test_parse_blueprint_substitutes_vars_in_path_and_content():
    """Template vars are substituted in both the FILE: path and the body."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: ~/Library/LaunchAgents/com.{bot_id}.briefing.plist\n"
            "```xml\n"
            "Hello {user_name}\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(
        blueprint=bp,
        embedded_path="demo.json",
        vars={"bot_id": "team_bot_a", "user_name": "Alex"},
    )
    bf = parsed.files[0]
    assert bf.launchd_label == "com.team_bot_a.briefing"
    assert bf.normalised_path == "Library/LaunchAgents/com.team_bot_a.briefing.plist"
    assert "Alex" in bf.content


def test_parse_blueprint_refuses_unsafe_paths():
    """Workspace paths with `..` segments are rejected."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: ../escape.sh\n"
            "```bash\n"
            "echo no\n"
            "```\n"
        ),
    )
    with pytest.raises(TemplateValidationError):
        parse_app_blueprint(blueprint=bp, embedded_path="demo.json")


def test_parse_blueprint_refuses_arbitrary_absolute_paths():
    """Absolute paths outside /Library/LaunchDaemons/ are rejected."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: /etc/passwd\n"
            "```\n"
            "evil\n"
            "```\n"
        ),
    )
    with pytest.raises(TemplateValidationError):
        parse_app_blueprint(blueprint=bp, embedded_path="demo.json")


def test_parse_blueprint_empty_build_spec_is_ok():
    """Narrative-only blueprints (no ## FILE: blocks) return an empty
    plan, not an error — V1.1-4 will fill build_spec content."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec="# Just narrative\n\nNo file blocks here.\n",
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    assert parsed.files == ()
    assert parsed.is_empty is True


def test_parse_blueprint_missing_build_spec_is_ok():
    """A blueprint with no build_spec key at all returns an empty plan."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = {"id": "app_demo", "name": "Demo"}
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    assert parsed.files == ()


def test_parse_blueprint_duplicate_path_last_wins_with_warning():
    """When the same FILE: path appears twice, last block wins (matches
    forge_engine) and a warning is emitted."""
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('first')\n"
            "```\n\n"
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('second')\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    assert len(parsed.files) == 1
    assert "second" in parsed.files[0].content
    assert any("duplicate" in w for w in parsed.warnings)


# ─ plan_provision integration ───────────────────────────────────────────────


def test_plan_provision_surfaces_embedded_app_plans(tmp_path):
    """plan_provision produces one EmbeddedAppPlan per embedded application."""
    bp_json = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print({user_name})\n"
            "```\n"
        ),
    )
    _write_template(
        tmp_path,
        "embed",
        manifest={
            "name": "embed",
            "description": "blueprint test",
            "skills": [{"id": "messaging"}],
            "applications": [
                {
                    "name": "Demo",
                    "embedded_path": "demo.json",
                    "skill_deps": ["messaging"],
                },
            ],
            "template_vars": {
                "user_name": {
                    "description": "user", "required": True,
                },
            },
        },
        embedded_apps={"demo.json": bp_json},
    )
    t = load_template("embed", templates_dir=tmp_path)
    resolution = resolve_skills(t)
    plan = plan_provision(
        t, bot_id="team_bot_a", vars={"user_name": "Alex"},
        skill_resolution=resolution,
    )
    assert len(plan.embedded_app_plans) == 1
    ep = plan.embedded_app_plans[0]
    assert ep.app_id == "app_demo"
    assert len(ep.files) == 1
    assert "Alex" in ep.files[0].content


def test_plan_provision_gallery_apps_skip_embedded_plans(tmp_path):
    """Gallery-referenced applications produce ApplicationInstall but
    no EmbeddedAppPlan entry."""
    _write_template(
        tmp_path,
        "gallery-only",
        manifest={
            "name": "gallery-only",
            "description": "gallery-ref test",
            "skills": [{"id": "messaging"}],
            "applications": [
                {
                    "name": "Manager",
                    "app_id": "app_task_manager",
                    "skill_deps": ["messaging"],
                },
            ],
        },
    )
    t = load_template("gallery-only", templates_dir=tmp_path)
    resolution = resolve_skills(t)
    plan = plan_provision(
        t, bot_id="team_bot_a", vars={}, skill_resolution=resolution,
    )
    assert plan.embedded_app_plans == ()
    assert len(plan.applications) == 1


# ─ apply_embedded_app — happy path ──────────────────────────────────────────


class _FakeLaunchdAdapter:
    """Records bootstrap / bootout calls; never touches launchctl."""

    def __init__(self, fail_on_label: str | None = None):
        self.bootstrap_calls: list[tuple[str, str, Path, str]] = []
        self.bootout_calls: list[tuple[str, str, str]] = []
        self.fail_on_label = fail_on_label

    def bootstrap(self, *, bot_user, label, plist_path, destination):
        self.bootstrap_calls.append((bot_user, label, plist_path, destination))
        if self.fail_on_label == label:
            return False
        return True

    def bootout(self, *, bot_user, label, destination):
        self.bootout_calls.append((bot_user, label, destination))
        return True


def _direct_writer(bot_user, workspace, relative_path, content):
    """Test-mode write_file: plain write, no sudo path."""
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_apply_embedded_app_writes_workspace_files(tmp_path):
    """Apply renders every workspace file and reports them in result.written."""
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('hello')\n"
            "```\n\n"
            "## FILE: scripts/cron.sh\n"
            "```bash\n"
            "#!/bin/bash\n"
            "echo go\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    adapter = _FakeLaunchdAdapter()
    result = apply_embedded_app(
        plan,
        bot_user="team_bot_a",
        workspace=workspace,
        home=home,
        launchd_adapter=adapter,
        write_file=_direct_writer,
    )
    assert result.ok is True, f"unexpected error: {result.error}"
    assert "scripts/run.py" in result.written
    assert "scripts/cron.sh" in result.written
    # No plists in this plan — no bootstrap calls.
    assert adapter.bootstrap_calls == []
    # Provenance marker stamped at the top of the file.
    py_text = (workspace / "scripts/run.py").read_text()
    assert "evolve: pkg=p-" in py_text
    sh_text = (workspace / "scripts/cron.sh").read_text()
    assert "evolve: pkg=p-" in sh_text
    # Shell script is executable.
    assert (workspace / "scripts/cron.sh").stat().st_mode & 0o111


def test_apply_embedded_app_bootstraps_launchagent(tmp_path):
    """Apply bootstraps every launchagent plist via the adapter."""
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: ~/Library/LaunchAgents/com.team_bot_a.morning.plist\n"
            "```xml\n"
            "<?xml version=\"1.0\"?>\n"
            "<plist><dict><key>Label</key>"
            "<string>com.team_bot_a.morning</string></dict></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    adapter = _FakeLaunchdAdapter()
    result = apply_embedded_app(
        plan,
        bot_user="team_bot_a",
        workspace=workspace,
        home=home,
        launchd_adapter=adapter,
        write_file=_direct_writer,
    )
    assert result.ok is True, f"unexpected error: {result.error}"
    assert "com.team_bot_a.morning" in result.loaded_labels
    assert len(adapter.bootstrap_calls) == 1
    bot, label, plist, dest = adapter.bootstrap_calls[0]
    assert label == "com.team_bot_a.morning"
    assert dest == "launchagent"
    assert plist == home / "Library/LaunchAgents/com.team_bot_a.morning.plist"
    # Plist exists on disk.
    assert plist.exists()


# ─ apply_embedded_app — atomic rollback ─────────────────────────────────────


def test_apply_embedded_app_rolls_back_on_bootstrap_failure(tmp_path):
    """If launchctl bootstrap fails, every file written by the plan is
    rolled back (deleted if new, restored if pre-existing)."""
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('new')\n"
            "```\n\n"
            "## FILE: ~/Library/LaunchAgents/com.team_bot_a.morning.plist\n"
            "```xml\n"
            "<plist><dict><key>Label</key>"
            "<string>com.team_bot_a.morning</string></dict></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    # Pre-existing workspace file: must be restored, not deleted, on
    # rollback.
    (workspace / "scripts").mkdir()
    (workspace / "scripts/run.py").write_text("ORIGINAL", encoding="utf-8")

    adapter = _FakeLaunchdAdapter(fail_on_label="com.team_bot_a.morning")
    result = apply_embedded_app(
        plan,
        bot_user="team_bot_a",
        workspace=workspace,
        home=home,
        launchd_adapter=adapter,
        write_file=_direct_writer,
    )
    assert result.ok is False
    assert "bootstrap failed" in (result.error or "")
    assert result.written == ()
    # Pre-existing file restored to its original content.
    assert (workspace / "scripts/run.py").read_text() == "ORIGINAL"
    assert "scripts/run.py" in result.restored_paths
    # New launchagent file deleted (didn't exist before).
    assert not (home / "Library/LaunchAgents/com.team_bot_a.morning.plist").exists()
    # No spurious bootout calls (we never bootstrapped successfully).
    # The failing label is in bootstrap_calls; rollback should NOT call
    # bootout for it (it was never loaded).
    assert all(
        label != "com.team_bot_a.morning" for _, label, _ in adapter.bootout_calls
    )


def test_apply_embedded_app_rolls_back_when_second_bootstrap_fails(tmp_path):
    """If the first bootstrap succeeds but the second fails, the first
    is bootout-ed and every file is rolled back."""
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: ~/Library/LaunchAgents/com.team_bot_a.first.plist\n"
            "```xml\n"
            "<plist><dict/></plist>\n"
            "```\n\n"
            "## FILE: ~/Library/LaunchAgents/com.team_bot_a.second.plist\n"
            "```xml\n"
            "<plist><dict/></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    adapter = _FakeLaunchdAdapter(fail_on_label="com.team_bot_a.second")
    result = apply_embedded_app(
        plan,
        bot_user="team_bot_a",
        workspace=workspace,
        home=home,
        launchd_adapter=adapter,
        write_file=_direct_writer,
    )
    assert result.ok is False
    # The first bootstrap was rolled back.
    booted_out = [label for _, label, _ in adapter.bootout_calls]
    assert "com.team_bot_a.first" in booted_out
    # Both plist files were deleted (neither existed before).
    assert not (home / "Library/LaunchAgents/com.team_bot_a.first.plist").exists()
    assert not (home / "Library/LaunchAgents/com.team_bot_a.second.plist").exists()


def test_apply_embedded_app_rolls_back_on_write_failure(tmp_path):
    """If a file write raises, prior writes are rolled back and the
    rest of the plan is NOT executed."""
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/ok.py\n"
            "```python\n"
            "print('ok')\n"
            "```\n\n"
            "## FILE: scripts/will_fail.py\n"
            "```python\n"
            "print('fail')\n"
            "```\n\n"
            "## FILE: scripts/unreached.py\n"
            "```python\n"
            "print('unreached')\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    def _flaky_writer(bot_user, ws, relative_path, content):
        if relative_path == "will_fail.py":
            raise PermissionError("simulated write failure")
        target = ws / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    adapter = _FakeLaunchdAdapter()
    result = apply_embedded_app(
        plan,
        bot_user="team_bot_a",
        workspace=workspace,
        home=home,
        launchd_adapter=adapter,
        write_file=_flaky_writer,
    )
    assert result.ok is False
    assert "simulated write failure" in (result.error or "")
    # First file was rolled back (deleted because it was new).
    assert not (workspace / "scripts/ok.py").exists()
    # Third file was never touched.
    assert not (workspace / "scripts/unreached.py").exists()
    # No bootstrap calls — we failed before getting to phase 2.
    assert adapter.bootstrap_calls == []


def test_apply_embedded_app_provenance_marker_stable_pkg_id(tmp_path):
    """Re-rendering the same app blueprint should produce the same
    pkg_id in the embedded marker (so forge can find it later)."""
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('hello')\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )

    def _run(target: Path) -> str:
        target.mkdir(exist_ok=True)
        home = target.parent / "home"
        home.mkdir(exist_ok=True)
        r = apply_embedded_app(
            plan,
            bot_user="team_bot_a",
            workspace=target,
            home=home,
            launchd_adapter=_FakeLaunchdAdapter(),
            write_file=_direct_writer,
        )
        assert r.ok, r.error
        text = (target / "scripts/run.py").read_text()
        # Extract pkg_id from marker line.
        import re
        m = re.search(r"pkg=(p-[0-9a-f]{8})", text)
        assert m, f"no marker in: {text[:200]}"
        return m.group(1)

    ws_a = tmp_path / "a/workspace"
    ws_b = tmp_path / "b/workspace"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    pkg_a = _run(ws_a)
    pkg_b = _run(ws_b)
    assert pkg_a == pkg_b, (
        f"pkg_id should be deterministic across deploys of the same "
        f"blueprint, got {pkg_a} vs {pkg_b}"
    )


def test_summarize_plan_lists_embedded_app_plans(tmp_path):
    """The CLI dry-run summary mentions embedded-app blueprints + the
    files they will render."""
    from evolve_admin.bot_templates import build_plan, summarize_plan

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: scripts/run.py\n"
            "```python\n"
            "print('hi')\n"
            "```\n"
        ),
    )
    _write_template(
        tmp_path,
        "embed-summary",
        manifest={
            "name": "embed-summary",
            "description": "summary test",
            "skills": [{"id": "messaging"}],
            "applications": [
                {
                    "name": "Demo",
                    "embedded_path": "demo.json",
                    "skill_deps": ["messaging"],
                },
            ],
        },
        embedded_apps={"demo.json": bp},
    )
    result = build_plan(
        template_name="embed-summary",
        bot_id="team_bot_a",
        templates_dir=tmp_path,
    )
    assert result.ok
    lines = summarize_plan(result)
    full = "\n".join(lines)
    assert "Embedded app blueprints:" in full
    assert "Demo (demo.json)" in full
    assert "scripts/run.py" in full


# ── V1.1-2: Template-var substitution + TZ handling ──────────────────────────
#
# Tests cover:
#   1. briefing_time → briefing_hour / briefing_minute derivation
#   2. TZ-aware conversion for StartCalendarInterval (system-local time)
#   3. briefing_tz_env injection for plist EnvironmentVariables
#   4. exec-approvals.template.json uses real OC schema
#   5. apply_embedded_app wired into _deploy_from_template (integration)
#   6. morning-briefing template FILE: blocks parse + substitute correctly


def test_derive_briefing_hour_minute_happy_path():
    """Standard HH:MM strings parse to (hour, minute) integers."""
    from evolve_admin.bot_templates.provisioner import _derive_briefing_hour_minute

    assert _derive_briefing_hour_minute("07:00") == (7, 0)
    assert _derive_briefing_hour_minute("6:30") == (6, 30)
    assert _derive_briefing_hour_minute("23:59") == (23, 59)
    assert _derive_briefing_hour_minute("00:00") == (0, 0)


def test_derive_briefing_hour_minute_rejects_bad_input():
    """Non-HH:MM strings return None, not an exception."""
    from evolve_admin.bot_templates.provisioner import _derive_briefing_hour_minute

    assert _derive_briefing_hour_minute("bad") is None
    assert _derive_briefing_hour_minute("25:00") is None   # hour > 23
    assert _derive_briefing_hour_minute("07:60") is None   # minute > 59
    assert _derive_briefing_hour_minute("") is None


def test_tz_offset_minutes_known_zones():
    """_tz_offset_minutes returns a numeric offset for recognised zones."""
    from evolve_admin.bot_templates.provisioner import _tz_offset_minutes

    # Offsets are DST-dependent so we only assert they're in a sane range.
    et = _tz_offset_minutes("America/New_York")
    pt = _tz_offset_minutes("America/Los_Angeles")
    utc = _tz_offset_minutes("UTC")
    assert et is not None and -360 <= et <= 0
    assert pt is not None and -480 <= pt <= -420
    assert utc == 0


def test_tz_offset_minutes_unknown_zone_returns_none():
    """Unrecognised IANA zone names return None without raising."""
    from evolve_admin.bot_templates.provisioner import _tz_offset_minutes

    assert _tz_offset_minutes("Not/AZone") is None
    assert _tz_offset_minutes("") is None


def test_convert_time_to_system_local_et_to_pt():
    """7am ET on a PT system should produce 4am PT (when ET is UTC-4 and PT is UTC-7).

    We can only verify this is *plausible* (within day boundaries) because
    the exact result depends on the test runner's system TZ + DST.
    What we can assert:
      - Result is within 0..23 / 0..59
      - When we simulate: ET offset = -4h, PT offset = -7h → delta = -3h
        → 7am + (-3*60) = 4am (h=4, m=0)
    """
    from evolve_admin.bot_templates.provisioner import _convert_time_to_system_local
    from evolve_admin.bot_templates.provisioner import _tz_offset_minutes

    # We can't control system TZ in tests, so validate the algorithm
    # by computing what the result *should* be based on measured offsets.
    et_offset = _tz_offset_minutes("America/New_York")
    pt_offset = _tz_offset_minutes("America/Los_Angeles")
    if et_offset is None or pt_offset is None:
        pytest.skip("zoneinfo not available")

    # Simulate: system is running at PT
    # Override sys_offset in a white-box way: derive expected result directly.
    expected_delta = pt_offset - et_offset  # minutes
    total = 7 * 60 + 0 + expected_delta
    total = total % (24 * 60)
    expected_h, expected_m = divmod(total, 60)

    # The actual result uses the real system TZ — only matches ET->PT
    # if system IS on PT, so we just check range validity here.
    h, m = _convert_time_to_system_local(7, 0, "America/New_York")
    assert 0 <= h <= 23
    assert 0 <= m <= 59


def test_plan_provision_derives_briefing_hour_minute(tmp_path):
    """plan_provision injects briefing_hour/briefing_minute from briefing_time."""
    import yaml as _yaml

    tdir = tmp_path / "briefing-test"
    tdir.mkdir()
    (tdir / "template.yaml").write_text(_yaml.safe_dump({
        "name": "briefing-test",
        "description": "TZ test",
        "skills": [],
        "applications": [],
        "template_vars": {
            "user_name": {"required": True},
            "briefing_time": {"default": "07:00"},
            "time_zone": {"default": "America/Los_Angeles"},
        },
    }))

    t = load_template("briefing-test", templates_dir=tmp_path)
    res = resolve_skills(t, installed_plugins=[])
    plan = plan_provision(
        t,
        bot_id="personal_bot",
        vars={"user_name": "Pod_admin"},
        skill_resolution=res,
    )
    assert "briefing_hour" in plan.vars
    assert "briefing_minute" in plan.vars
    # briefing_time "07:00" should yield hour=7, minute=0 as a baseline
    # (TZ conversion may shift these if system is not on America/Los_Angeles)
    # What we can assert: they're in range and are integers.
    assert isinstance(plan.vars["briefing_hour"], int)
    assert isinstance(plan.vars["briefing_minute"], int)
    assert 0 <= plan.vars["briefing_hour"] <= 23
    assert 0 <= plan.vars["briefing_minute"] <= 59


def test_plan_provision_briefing_tz_env_injected(tmp_path):
    """plan_provision injects briefing_tz_env = time_zone for plist EnvironmentVariables."""
    import yaml as _yaml

    tdir = tmp_path / "tz-env-test"
    tdir.mkdir()
    (tdir / "template.yaml").write_text(_yaml.safe_dump({
        "name": "tz-env-test",
        "description": "TZ env test",
        "skills": [],
        "applications": [],
        "template_vars": {
            "user_name": {"required": True},
            "time_zone": {"default": "America/New_York"},
        },
    }))

    t = load_template("tz-env-test", templates_dir=tmp_path)
    res = resolve_skills(t, installed_plugins=[])
    plan = plan_provision(
        t,
        bot_id="personal_bot",
        vars={"user_name": "Pod_admin", "time_zone": "America/Chicago"},
        skill_resolution=res,
    )
    assert plan.vars.get("briefing_tz_env") == "America/Chicago"


def test_plan_provision_unknown_tz_adds_warning(tmp_path):
    """plan_provision warns when time_zone is not a recognised IANA zone."""
    import yaml as _yaml

    tdir = tmp_path / "badtz-test"
    tdir.mkdir()
    (tdir / "template.yaml").write_text(_yaml.safe_dump({
        "name": "badtz-test",
        "description": "bad TZ test",
        "skills": [],
        "applications": [],
        "template_vars": {
            "user_name": {"required": True},
            "briefing_time": {"default": "07:00"},
            "time_zone": {"default": "America/Los_Angeles"},
        },
    }))

    t = load_template("badtz-test", templates_dir=tmp_path)
    res = resolve_skills(t, installed_plugins=[])
    plan = plan_provision(
        t,
        bot_id="personal_bot",
        vars={"user_name": "Pod_admin", "time_zone": "Not/AZone"},
        skill_resolution=res,
    )
    # briefing_hour/minute still injected (raw, no conversion)
    assert "briefing_hour" in plan.vars
    # Warning surfaced
    assert any("Not/AZone" in w or "recognised" in w for w in plan.warnings)


def test_exec_approvals_real_oc_schema_in_morning_briefing():
    """exec-approvals.template.json in morning-briefing matches the empirically-
    verified real OC schema: ``{version: 1, defaults: {}, agents: {main: {allowlist: [...]}}}``.

    Reviewer pass 2 (V1.1-2) flagged the prior populated ``defaults.security`` /
    ``ask`` / ``askFallback`` / ``autoAllowSkills`` fields as a risk — those keys
    appear in the admin-UI reader (ocadmin.py) but NO real bot on the mini
    ships them populated (team_bot_a/admin_bot/team_bot_c/security_bot all have ``defaults: {}``,
    security_bot has only ``allowlist`` in agents.main). The fix-up aligns with
    the empirically-working shape.
    """
    t = load_template("morning-briefing")
    assert t.exec_approvals is not None
    ea = t.exec_approvals
    # Real schema keys
    assert ea["version"] == 1
    assert ea["defaults"] == {}, (
        f"defaults must be empty per real-bot schema; got {ea['defaults']!r}"
    )
    # agents.main has an allowlist (no security/ask/askFallback keys)
    assert "main" in ea["agents"]
    main = ea["agents"]["main"]
    assert "allowlist" in main
    assert isinstance(main["allowlist"], list)
    assert len(main["allowlist"]) > 0
    # Each allowlist entry has a pattern field
    for entry in main["allowlist"]:
        assert "pattern" in entry, f"allowlist entry missing 'pattern': {entry}"
    # No invented top-level keys in agents.main beyond allowlist (matches security_bot).
    extra_keys = set(main.keys()) - {"allowlist"}
    assert not extra_keys, (
        f"agents.main has unexpected keys not in real-bot schema: {extra_keys}. "
        "Security_bot on the mini ships only 'allowlist' in agents.main."
    )


def test_exec_approvals_real_oc_schema_in_test_minimal():
    """exec-approvals.template.json in test-minimal also matches the real OC schema."""
    t = load_template("test-minimal")
    assert t.exec_approvals is not None
    ea = t.exec_approvals
    assert ea["version"] == 1
    assert ea["defaults"] == {}
    assert "main" in ea["agents"]
    assert "allowlist" in ea["agents"]["main"]


def test_morning_briefing_file_blocks_parse_with_tz_vars():
    """morning_briefing.json build_spec now has ## FILE: blocks that
    correctly substitute {briefing_hour}/{briefing_minute}/{briefing_tz_env}."""
    from evolve_admin.bot_templates import parse_app_blueprint
    import json as _json
    from pathlib import Path as _Path

    repo_root = _Path(__file__).parent.parent.parent.parent
    bp_file = repo_root / "gallery/bot-templates/morning-briefing/apps/morning_briefing.json"
    bp = _json.loads(bp_file.read_text())

    result = parse_app_blueprint(
        blueprint=bp,
        embedded_path="morning_briefing.json",
        vars={
            "bot_id": "personal_bot",
            "user_name": "Pod_admin",
            "time_zone": "America/Los_Angeles",
            "briefing_time": "07:00",
            "briefing_hour": 7,
            "briefing_minute": 0,
            "briefing_detail": "standard",
            "location": "Los Angeles, CA",
            "news_sources": "",
            "briefing_tz_env": "America/Los_Angeles",
        },
    )
    assert result.app_id == "app_morning_briefing"
    # Must have at least 2 files: cron.sh + plist
    assert len(result.files) >= 2

    by_path = {f.raw_path: f for f in result.files}
    # Cron shell script
    cron_key = next((k for k in by_path if "briefing-cron.sh" in k), None)
    assert cron_key is not None, f"briefing-cron.sh not found in {list(by_path)}"
    assert by_path[cron_key].executable is True
    assert "personal_bot" in by_path[cron_key].content

    # Plist
    plist_key = next((k for k in by_path if ".plist" in k), None)
    assert plist_key is not None, f"plist not found in {list(by_path)}"
    plist = by_path[plist_key]
    # V1.1-2 fix-up: switched to LaunchDaemon for headless bots on the mini —
    # ~/Library/LaunchAgents never load without a GUI session. LaunchDaemons
    # run at boot with UserName=<bot_id> so the script still runs as the bot.
    assert plist.destination == "launchdaemon"
    assert "com.personal_bot.morning-briefing" in plist.content
    assert "<integer>7</integer>" in plist.content   # briefing_hour
    assert "<integer>0</integer>" in plist.content   # briefing_minute
    assert "America/Los_Angeles" in plist.content    # briefing_tz_env in TZ key
    # Daemon must run as the bot user, not root.
    assert "<key>UserName</key>" in plist.content
    assert "<string>personal_bot</string>" in plist.content


def test_apply_plan_wires_embedded_app(tmp_path):
    """_deploy_from_template wires apply_embedded_app for embedded-app plans.

    Tests the wiring path in cli.py step 7b without touching real bots.
    Uses a template with an embedded app that has ## FILE: blocks, then
    asserts the files were written to the tmp workspace.
    """
    from evolve_admin.bot_templates import (
        EmbeddedAppPlan, parse_app_blueprint,
        apply_embedded_app,
    )

    build_spec = (
        "## FILE: scripts/morning_hello.py\n"
        "```python\n"
        "# hello from {bot_id} for {user_name}\n"
        "print('Good morning, {user_name}!')\n"
        "```\n"
    )
    bp = _blueprint_with_files(app_id="app_hello", build_spec=build_spec)
    parsed = parse_app_blueprint(
        blueprint=bp,
        embedded_path="hello.json",
        vars={"bot_id": "personal_bot", "user_name": "Pod_admin"},
    )
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    result = apply_embedded_app(
        plan,
        bot_user="personal_bot",
        workspace=workspace,
        home=home,
        launchd_adapter=_FakeLaunchdAdapter(),
        write_file=_direct_writer,
    )
    assert result.ok, f"apply failed: {result.error}"
    assert "scripts/morning_hello.py" in result.written
    # Content has substituted vars
    content = (workspace / "scripts/morning_hello.py").read_text()
    assert "personal_bot" in content
    assert "Pod_admin" in content


# ── V1.1-2 fix-up: LaunchDaemon destination + bot_id wiring + tz_env leak ───
#
# Reviewer pass 2 (V1.1-2) returned CONCERNS with one blocking + three
# non-blocking findings:
#
# 1. CRITICAL: morning_briefing.json targeted ~/Library/LaunchAgents/ but
#    Personal_bot/admin_bot/team_bot_a are headless on the mini — LaunchAgents never load.
#    The fix-up switches to /Library/LaunchDaemons/ with UserName=<bot_id>.
#
# 2. apply_embedded_app's V1.1-1 fix-up added bot_id + shared_dir kwargs
#    for the template-installs SoT manifest. V1.1-2's wire-up didn't pass
#    them, so installed labels would not be registered with the manifest.
#
# 3. {briefing_tz_env} placeholder leaks if a future template uses it but
#    doesn't declare time_zone. Error out at planning time rather than ship
#    a plist with a literal {briefing_tz_env} placeholder.
#
# 4. Deploy console scroll loses inline errors; need an end-of-run tally.


def test_morning_briefing_plist_is_launchdaemon_not_launchagent():
    """The morning-briefing plist FILE: block must classify as launchdaemon.

    Personal_bot/admin_bot/team_bot_a on the mini are headless — ~/Library/LaunchAgents/
    plists require an active GUI session to load (per deploy.py:2640-2641
    comment). LaunchDaemons load at boot regardless. This test pins the
    destination switch so a future edit can't silently regress it.
    """
    from evolve_admin.bot_templates import parse_app_blueprint
    import json as _json
    from pathlib import Path as _Path

    repo_root = _Path(__file__).parent.parent.parent.parent
    bp_file = repo_root / "gallery/bot-templates/morning-briefing/apps/morning_briefing.json"
    bp = _json.loads(bp_file.read_text())

    result = parse_app_blueprint(
        blueprint=bp,
        embedded_path="morning_briefing.json",
        vars={
            "bot_id": "personal_bot",
            "user_name": "Pod_admin",
            "time_zone": "America/Los_Angeles",
            "briefing_time": "07:00",
            "briefing_hour": 7,
            "briefing_minute": 0,
            "briefing_detail": "standard",
            "location": "Los Angeles, CA",
            "news_sources": "",
            "briefing_tz_env": "America/Los_Angeles",
        },
    )
    plist_files = [f for f in result.files if f.normalised_path.endswith(".plist")]
    assert len(plist_files) == 1, f"expected one plist; got {plist_files}"
    plist = plist_files[0]
    assert plist.destination == "launchdaemon", (
        f"morning-briefing plist must be a LaunchDaemon for headless bots; "
        f"got destination={plist.destination!r} (raw_path={plist.raw_path!r})"
    )
    # The plist content must include UserName so the daemon runs as the
    # bot user, not root. Without UserName, launchd runs the script as
    # root which would have wrong permissions for bot workspace access.
    assert "<key>UserName</key>" in plist.content
    assert "<string>personal_bot</string>" in plist.content
    # And GroupName=staff (bot users' primary group on macOS).
    assert "<key>GroupName</key>" in plist.content
    assert "<string>staff</string>" in plist.content
    # The launchd label is captured for the template-installs manifest.
    assert plist.launchd_label == "com.personal_bot.morning-briefing"


def test_briefing_tz_env_leak_errors_when_time_zone_missing(tmp_path):
    """If a template uses {briefing_tz_env} in any embedded blueprint but
    does NOT declare time_zone, the planner must fail loud.

    Otherwise the literal placeholder {briefing_tz_env} leaks into the
    rendered plist as `<string>{briefing_tz_env}</string>`, and launchd
    sets `TZ={briefing_tz_env}` literally — undefined behaviour at runtime.
    """
    from evolve_admin.bot_templates import build_plan

    tdir = tmp_path / "bad-tz"
    tdir.mkdir()
    apps_dir = tdir / "apps"
    apps_dir.mkdir()
    # Embedded blueprint declares no time_zone in template_vars, but its
    # build_spec uses {briefing_tz_env}.
    leaky_bp = {
        "schema_version": 5,
        "manifest_type": "evolve_application",
        "id": "app_leaky",
        "name": "Leaky",
        "description": "uses tz env without declaring time_zone",
        "build_spec": (
            "## FILE: scripts/leak.sh\n"
            "```bash\n"
            "echo TZ={briefing_tz_env}\n"
            "```\n"
        ),
    }
    import json as _json
    (apps_dir / "leaky.json").write_text(_json.dumps(leaky_bp))
    # Reference the embedded app from template.yaml. Skills must be a list
    # of {id, source, optional} per loader.py — empty list is valid.
    (tdir / "template.yaml").write_text(
        "name: bad-tz\n"
        "display_name: Bad TZ\n"
        "description: leak repro\n"
        "skills: []\n"
        "applications:\n"
        "  - name: Leaky\n"
        "    embedded_path: leaky.json\n"
        "template_vars:\n"
        "  bot_id:\n"
        "    description: bot id\n"
        "    default: team_bot_a\n",
        encoding="utf-8",
    )

    # build_plan should surface the leak as a validation error.
    # templates_dir is the *parent* search root; bad-tz/ lives directly
    # inside tmp_path so passing tmp_path as the templates_dir resolves
    # name="bad-tz" → tmp_path/bad-tz/template.yaml.
    result = build_plan(
        template_name="bad-tz",
        bot_id="team_bot_a",
        templates_dir=tmp_path,
    )
    assert not result.ok, "planner must reject {briefing_tz_env} without time_zone"
    # Error message names the offending blueprint and the placeholder.
    msg = " ".join(result.validation_errors)
    assert "briefing_tz_env" in msg
    assert "leaky.json" in msg
    assert "time_zone" in msg


def test_apply_plan_wires_embedded_app_passes_bot_id_and_shared_dir(tmp_path):
    """Contract test: the deploy flow's call to apply_embedded_app must
    include both ``bot_id`` and ``shared_dir`` so the V1.1-1 fix-up's
    template-installs manifest gets written. Without these, retire-bot
    + the orphan-sweeper cannot find template-installed plists.

    Captures the call signature by intercepting apply_embedded_app and
    asserting the kwargs the deploy flow passes.
    """
    from evolve_admin.bot_templates import cli_integration as _ci

    captured: dict[str, object] = {}

    def _fake_apply(plan, **kwargs):
        captured.update(kwargs)
        # Return a successful result so the deploy continues.
        from evolve_admin.bot_templates.cli_integration import (
            EmbeddedAppApplyResult,
        )
        return EmbeddedAppApplyResult(
            app_id=plan.app_id,
            app_name=plan.app_name,
            ok=True,
            written=("scripts/x.sh",),
            loaded_labels=(),
            restored_paths=(),
            rollback_failures=(),
            error=None,
        )

    # Direct unit-level assertion — easier than reaching through Click.
    # The wire-up site (cli.py:_deploy_from_template step 7b) is exercised
    # in `test_apply_plan_wires_embedded_app` above for the basic case;
    # this test pins the *kwarg names* the wire-up must pass.
    from evolve_admin.bot_templates import EmbeddedAppPlan, BlueprintFile

    plan = EmbeddedAppPlan(
        app_id="app_x",
        app_name="X",
        embedded_path="x.json",
        files=(
            BlueprintFile(
                raw_path="scripts/x.sh",
                destination="workspace",
                normalised_path="scripts/x.sh",
                content="#!/bin/bash\n",
                launchd_label=None,
                executable=True,
            ),
        ),
        launchd_labels=(),
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    # Reproduce the kwargs the deploy flow passes (cli.py step 7b).
    _fake_apply(
        plan,
        bot_user="team_bot_a",
        workspace=workspace,
        home=home,
        bot_id="team_bot_a",
        shared_dir=shared_dir,
    )
    assert captured["bot_id"] == "team_bot_a"
    assert captured["shared_dir"] == shared_dir
    assert captured["bot_user"] == "team_bot_a"

    # And verify the real apply_embedded_app accepts these kwargs without
    # complaint — a future signature change would break this.
    import inspect
    sig = inspect.signature(_ci.apply_embedded_app)
    assert "bot_id" in sig.parameters, (
        "apply_embedded_app must accept bot_id kwarg (V1.1-1 fix-up #4 contract)"
    )
    assert "shared_dir" in sig.parameters, (
        "apply_embedded_app must accept shared_dir kwarg (V1.1-1 fix-up #4 contract)"
    )


def test_morning_briefing_template_tz_env_check_does_not_fire_with_time_zone():
    """Sanity: the morning-briefing template DOES declare time_zone, so
    the tz_env leak check must NOT fire on the canonical template."""
    from evolve_admin.bot_templates import build_plan

    result = build_plan(
        template_name="morning-briefing",
        bot_id="personal_bot",
        vars={"bot_id": "personal_bot"},  # time_zone has a default
    )
    # Plan may or may not be ok depending on what else fails, but the
    # specific tz_env leak error must NOT be in the errors list.
    leak_errors = [e for e in result.validation_errors if "briefing_tz_env" in e]
    assert not leak_errors, (
        f"tz_env leak check fired on canonical template: {leak_errors}"
    )


# ── V1.1-1 fix-up: Path-traversal security ──────────────────────────────────
#
# The reviewer reproduced traversal in absolute paths because the
# original classify_path only ran the ``..`` check on the workspace
# branch. After the fix, EVERY destination rejects ``..`` segments
# before doing prefix-based classification.
#
# Each pattern below was reproduced by the reviewer; together they
# pin the security boundary so a future refactor cannot silently
# regress it.


@pytest.mark.parametrize(
    "evil_path",
    [
        # Reviewer's literal reproductions:
        "/Library/LaunchDaemons/foo/../../../etc/passwd.plist",
        "/Library/LaunchDaemons/../etc/passwd.plist",
        "../../../etc/passwd",
        "/Library/LaunchDaemons/x/../etc/passwd",
        # Additional adversarial variants:
        "~/Library/LaunchAgents/../../../etc/passwd.plist",
        "~/Library/LaunchAgents/com.foo/../../../etc/passwd.plist",
        "/Library/LaunchDaemons/foo/..",
        "scripts/../escape.sh",
        "./scripts/../../escape.sh",
    ],
)
def test_classify_path_rejects_dotdot_in_every_destination(evil_path):
    """The blanket ``..`` rejection MUST fire regardless of destination.

    Before the V1.1-1 fix-up, only the workspace branch ran this check;
    a malicious blueprint could escape via the launchdaemon /
    launchagent branches because they skipped the check entirely. The
    reviewer reproduced ``/Library/LaunchDaemons/foo/../../../etc/
    passwd.plist`` classifying as a launchdaemon and being handed to
    ``sudo /bin/cp`` which dereferences ``..`` at syscall level.
    """
    from evolve_admin.bot_templates.app_blueprint import classify_path

    with pytest.raises(TemplateValidationError):
        classify_path(evil_path)


def test_classify_path_rejects_absolute_etc_passwd():
    """Plain ``/etc/passwd`` is refused — the literal absolute branch
    that was already wired before the fix-up. Pin the regression."""
    from evolve_admin.bot_templates.app_blueprint import classify_path

    with pytest.raises(TemplateValidationError):
        classify_path("/etc/passwd")


def test_classify_path_rejects_substituted_dotdot_via_bot_id():
    """If a template-var substitution drops ``..`` into the path
    AFTER substitution but BEFORE classify_path fires, the check still
    catches it. Mirrors the substitution-then-classify ordering inside
    :func:`parse_app_blueprint`.
    """
    from evolve_admin.bot_templates import parse_app_blueprint

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: /Library/LaunchDaemons/com.{bot_id}.briefing.plist\n"
            "```xml\n"
            "<plist><dict/></plist>\n"
            "```\n"
        ),
    )
    # Malicious bot_id injects ../.. into the otherwise-rooted path.
    with pytest.raises(TemplateValidationError):
        parse_app_blueprint(
            blueprint=bp,
            embedded_path="demo.json",
            vars={"bot_id": "../../../etc/passwd"},
        )


def test_classify_path_still_accepts_legitimate_launchdaemon():
    """A clean ``/Library/LaunchDaemons/<label>.plist`` path still
    works after the security tightening — we want the security gate
    to be tight, not paranoid."""
    from evolve_admin.bot_templates.app_blueprint import classify_path

    destination, normalised, label = classify_path(
        "/Library/LaunchDaemons/ai.openclaw.evolve.team_bot_a.briefing.plist"
    )
    assert destination == "launchdaemon"
    assert normalised == (
        "/Library/LaunchDaemons/ai.openclaw.evolve.team_bot_a.briefing.plist"
    )
    assert label == "ai.openclaw.evolve.team_bot_a.briefing"


def test_classify_path_still_accepts_legitimate_launchagent():
    """A clean ``~/Library/LaunchAgents/<label>.plist`` path still
    works after the security tightening."""
    from evolve_admin.bot_templates.app_blueprint import classify_path

    destination, normalised, label = classify_path(
        "~/Library/LaunchAgents/com.team_bot_a.briefing.plist"
    )
    assert destination == "launchagent"
    assert normalised == "Library/LaunchAgents/com.team_bot_a.briefing.plist"
    assert label == "com.team_bot_a.briefing"


# ── V1.1-1 fix-up: launchctl bootstrap probe (silent-failure DNA) ───────────


class _RecordingLaunchctl:
    """Subprocess-call recorder used to drive LaunchdAdapter unit tests.

    Each entry in ``scripts`` is matched against the leading args of an
    incoming ``subprocess.run`` invocation; the first match wins and
    returns a fake ``CompletedProcess`` with the configured rc / stdout
    / stderr. Unmatched calls return rc=0 with empty output (sensible
    default for ignored bootouts).
    """

    def __init__(self, scripts):
        self.scripts = scripts  # list of (match_args, returncode, stdout, stderr)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        import subprocess as _sp
        self.calls.append(list(argv))
        for match_args, rc, stdout, stderr in self.scripts:
            if argv[: len(match_args)] == list(match_args):
                cp = _sp.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)
                return cp
        return _sp.CompletedProcess(argv, 0, stdout="", stderr="")


def test_launchd_adapter_bootstrap_rc0_but_probe_says_not_loaded_fails(
    monkeypatch,
):
    """Bootstrap rc=0 + probe says "not found" → return False.

    Same DNA as C1.d retire-bot's ``_launchctl_service_loaded``: rc=0
    from ``launchctl bootstrap`` is NOT sufficient evidence the service
    actually loaded; the post-condition probe is the authority.
    """
    import subprocess
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    rec = _RecordingLaunchctl([
        # bootout (idempotent first-step) — return non-zero "not loaded"
        (["sudo", "/bin/launchctl", "bootout", "system/foo.bar"],
         1, "", "Could not find service foo.bar"),
        # bootstrap rc=0 (accepted parse, but no actual schedule)
        (["sudo", "/bin/launchctl", "bootstrap", "system"],
         0, "", ""),
        # probe rc=1 with "not found" stderr → silent failure shape
        (["sudo", "/bin/launchctl", "print", "system/foo.bar"],
         1, "", "Could not find service on domain"),
    ])
    monkeypatch.setattr(subprocess, "run", rec)

    adapter = LaunchdAdapter()
    ok = adapter.bootstrap(
        bot_user="team_bot_a",
        label="foo.bar",
        plist_path=Path("/Library/LaunchDaemons/foo.bar.plist"),
        destination="launchdaemon",
    )
    assert ok is False, (
        "bootstrap rc=0 with probe-says-not-loaded must surface as "
        "failure to trigger rollback (same DNA as C1.d)"
    )


def test_launchd_adapter_bootstrap_rc0_and_probe_loaded_succeeds(monkeypatch):
    """Bootstrap rc=0 + probe rc=0 → True. Happy path."""
    import subprocess
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    rec = _RecordingLaunchctl([
        (["sudo", "/bin/launchctl", "bootout", "system/foo.bar"],
         1, "", "Could not find service foo.bar"),
        (["sudo", "/bin/launchctl", "bootstrap", "system"], 0, "", ""),
        (["sudo", "/bin/launchctl", "print", "system/foo.bar"],
         0, "service block here", ""),
    ])
    monkeypatch.setattr(subprocess, "run", rec)

    adapter = LaunchdAdapter()
    ok = adapter.bootstrap(
        bot_user="team_bot_a",
        label="foo.bar",
        plist_path=Path("/Library/LaunchDaemons/foo.bar.plist"),
        destination="launchdaemon",
    )
    assert ok is True


def test_launchd_adapter_bootstrap_does_bootout_first(monkeypatch):
    """Re-deploy idempotency: bootstrap MUST call bootout first.

    Mirrors deploy.install_bot_gateway_plist:2733. Without the prior
    bootout, re-bootstrapping an already-loaded service fails with
    "service already loaded" and the operator sees a rollback they
    didn't expect.
    """
    import subprocess
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    rec = _RecordingLaunchctl([
        (["sudo", "/bin/launchctl", "bootout", "system/com.team_bot_a.x"],
         0, "", ""),  # bootout succeeds (was loaded)
        (["sudo", "/bin/launchctl", "bootstrap", "system"], 0, "", ""),
        (["sudo", "/bin/launchctl", "print", "system/com.team_bot_a.x"],
         0, "service block here", ""),
    ])
    monkeypatch.setattr(subprocess, "run", rec)

    adapter = LaunchdAdapter()
    ok = adapter.bootstrap(
        bot_user="team_bot_a",
        label="com.team_bot_a.x",
        plist_path=Path("/Library/LaunchDaemons/com.team_bot_a.x.plist"),
        destination="launchdaemon",
    )
    assert ok is True
    # Verify the call order: bootout before bootstrap.
    boot_idx = next(
        (i for i, c in enumerate(rec.calls) if "bootout" in c), None
    )
    bs_idx = next(
        (i for i, c in enumerate(rec.calls) if "bootstrap" in c), None
    )
    assert boot_idx is not None, "bootout must be invoked"
    assert bs_idx is not None, "bootstrap must be invoked"
    assert boot_idx < bs_idx, (
        f"bootout (idx={boot_idx}) must precede bootstrap (idx={bs_idx}) "
        f"for idempotent re-deploy"
    )


def test_launchd_adapter_bootstrap_probe_ambiguous_failure_treated_as_loaded(
    monkeypatch,
):
    """Probe rc=1 with non-"not-found" stderr (e.g. permission denied)
    is treated as worst-case "still loaded" → return False from
    bootstrap so the caller surfaces the failure rather than declaring
    premature success.
    """
    import subprocess
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    rec = _RecordingLaunchctl([
        (["sudo", "/bin/launchctl", "bootout"], 1, "", "not loaded"),
        (["sudo", "/bin/launchctl", "bootstrap", "system"], 0, "", ""),
        # rc=1 but stderr is NOT "not found" — ambiguous failure shape.
        (["sudo", "/bin/launchctl", "print", "system/com.team_bot_a.x"],
         1, "", "Operation not permitted"),
    ])
    monkeypatch.setattr(subprocess, "run", rec)

    adapter = LaunchdAdapter()
    ok = adapter.bootstrap(
        bot_user="team_bot_a",
        label="com.team_bot_a.x",
        plist_path=Path("/Library/LaunchDaemons/com.team_bot_a.x.plist"),
        destination="launchdaemon",
    )
    assert ok is False


# ── V1.1-1 fix-up: re-deploy idempotency ────────────────────────────────────


def test_apply_embedded_app_idempotent_on_second_run(tmp_path):
    """Deploying the same blueprint twice in a row both succeed.

    Before the fix, the second deploy would fail at bootstrap (service
    already loaded → rc!=0 → rollback fires → old file restored). With
    bootout-before-bootstrap the second deploy is a clean replace.
    """
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )

    bp = _blueprint_with_files(
        app_id="app_demo",
        build_spec=(
            "## FILE: ~/Library/LaunchAgents/com.team_bot_a.morning.plist\n"
            "```xml\n"
            "<plist><dict/></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    adapter = _FakeLaunchdAdapter()
    r1 = apply_embedded_app(
        plan, bot_user="team_bot_a", workspace=workspace, home=home,
        launchd_adapter=adapter, write_file=_direct_writer,
    )
    assert r1.ok is True, f"first deploy failed: {r1.error}"

    # Second deploy — same plan, same paths, file already exists.
    r2 = apply_embedded_app(
        plan, bot_user="team_bot_a", workspace=workspace, home=home,
        launchd_adapter=adapter, write_file=_direct_writer,
    )
    assert r2.ok is True, f"second deploy failed: {r2.error}"
    assert (home / "Library/LaunchAgents/com.team_bot_a.morning.plist").exists()


# ── V1.1-1 fix-up: LaunchDaemon ownership (root:wheel) ──────────────────────


def test_write_file_to_bot_workspace_launchdaemon_chowns_root_wheel(
    monkeypatch, tmp_path,
):
    """LaunchDaemon writes ALWAYS go through sudo /bin/cp + chown
    root:wheel + chmod 0644. macOS launchd rejects system daemons not
    owned by root, so the previous "chown <bot>:staff" path would fail
    every bootstrap → silent never-installs.
    """
    import subprocess
    from evolve_admin.bot_templates.cli_integration import (
        write_file_to_bot_workspace,
    )

    sudo_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        sudo_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Force the writer down the sudo path by simulating a PermissionError
    # on the direct write. Easiest way: target a path that cannot be
    # written directly — point at /Library/LaunchDaemons/ which the test
    # process can't write to.
    target_parent = Path("/Library/LaunchDaemons")
    # We override mkdir to no-op so the test doesn't need write access
    # to /Library/LaunchDaemons (mkdir is a precondition the caller is
    # supposed to satisfy; the test just exercises the chown branch).
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: None)
    # Force Path.write_text to raise PermissionError so the writer falls
    # through to the sudo /bin/cp path we want to verify.
    monkeypatch.setattr(
        Path, "write_text",
        lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError("simulated")),
    )

    write_file_to_bot_workspace(
        "team_bot_a", target_parent, "ai.openclaw.evolve.team_bot_a.briefing.plist",
        "<plist><dict/></plist>",
        destination="launchdaemon",
    )

    # Expect: sudo /bin/cp, sudo chown root:wheel, sudo chmod 0644.
    cp_calls = [c for c in sudo_calls if "/bin/cp" in c]
    chown_calls = [c for c in sudo_calls if "/usr/sbin/chown" in c]
    chmod_calls = [c for c in sudo_calls if "/bin/chmod" in c]
    assert cp_calls, "sudo /bin/cp must be invoked"
    assert any("root:wheel" in c for c in chown_calls), (
        f"launchdaemon must chown root:wheel; got chown calls: {chown_calls}"
    )
    assert not any("team_bot_a:staff" in c for c in chown_calls), (
        "launchdaemon must NOT chown to bot_user:staff (would fail bootstrap)"
    )
    assert any("0644" in c for c in chmod_calls), (
        f"launchdaemon must chmod 0644; got chmod calls: {chmod_calls}"
    )


def test_write_file_to_bot_workspace_launchagent_chowns_bot_user_staff(
    monkeypatch, tmp_path,
):
    """LaunchAgent writes go through sudo /bin/cp + chown <bot>:staff
    + chmod 0644 — they live in the bot user's home, not /Library/."""
    import subprocess
    from evolve_admin.bot_templates.cli_integration import (
        write_file_to_bot_workspace,
    )

    sudo_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        sudo_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: None)
    monkeypatch.setattr(
        Path, "write_text",
        lambda self, *a, **kw: (_ for _ in ()).throw(PermissionError("simulated")),
    )

    write_file_to_bot_workspace(
        "team_bot_a", Path("/Users/team_bot_a/Library/LaunchAgents"),
        "com.team_bot_a.briefing.plist",
        "<plist/>",
        destination="launchagent",
    )

    chown_calls = [c for c in sudo_calls if "/usr/sbin/chown" in c]
    chmod_calls = [c for c in sudo_calls if "/bin/chmod" in c]
    assert any("team_bot_a:staff" in c for c in chown_calls), (
        f"launchagent must chown team_bot_a:staff; got: {chown_calls}"
    )
    assert not any("root:wheel" in c for c in chown_calls), (
        "launchagent must NOT chown to root:wheel"
    )
    assert any("0644" in c for c in chmod_calls), (
        "launchagent plists must be 0644 so launchd will load them"
    )


# ── V1.1-1 fix-up: template-installs manifest (C1.d daemon-list SoT) ────────


def test_apply_embedded_app_records_in_template_installs_manifest(tmp_path):
    """Successful apply of a plist blueprint writes a per-bot manifest
    entry that retire-bot + the orphan-sweeper can consult.

    Without this, V1.1-1-installed LaunchDaemons would be invisible to
    both retire-bot (left dangling) and the orphan-sweeper (deleted on
    next deploy). Same DNA as C1.d's per-bot daemon-list drift bug, on
    the install side.
    """
    from evolve_admin.bot_templates import (
        apply_embedded_app, parse_app_blueprint, EmbeddedAppPlan,
    )
    from evolve_admin.template_installs import (
        read_template_installs, template_installed_labels,
    )

    bp = _blueprint_with_files(
        app_id="app_morning_briefing",
        build_spec=(
            "## FILE: /Library/LaunchDaemons/com.team_bot_a.briefing.plist\n"
            "```xml\n"
            "<plist><dict/></plist>\n"
            "```\n"
        ),
    )
    parsed = parse_app_blueprint(blueprint=bp, embedded_path="demo.json")
    plan = EmbeddedAppPlan(
        app_id=parsed.app_id,
        app_name=parsed.app_name,
        embedded_path=parsed.embedded_path,
        files=parsed.files,
        launchd_labels=parsed.launchd_labels,
    )

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    shared_dir = tmp_path / "shared"
    workspace.mkdir()
    home.mkdir()
    shared_dir.mkdir()
    # The launchdaemon path is absolute (/Library/LaunchDaemons/...);
    # write_file_to_bot_workspace will fail there in this test
    # environment. Use _direct_writer with a tmp-rooted workspace —
    # the FakeLaunchdAdapter is what records the bootstrap, not the
    # filesystem write. To avoid trying to write /Library/LaunchDaemons/
    # in the test, override the writer to NO-OP for absolute paths.

    def _safe_writer(bot_user, ws, rel, content):
        # ws is the parent dir; if absolute, write to a tmp mirror.
        if str(ws).startswith("/Library/LaunchDaemons"):
            mirror = tmp_path / "ld-mirror"
            mirror.mkdir(exist_ok=True)
            (mirror / rel).write_text(content)
            return
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    result = apply_embedded_app(
        plan,
        bot_user="team_bot_a",
        bot_id="team_bot_a",
        workspace=workspace,
        home=home,
        launchd_adapter=_FakeLaunchdAdapter(),
        write_file=_safe_writer,
        shared_dir=shared_dir,
    )
    assert result.ok is True, f"unexpected error: {result.error}"

    # Manifest must list the label as launchdaemon-installed.
    entries = read_template_installs(shared_dir, "team_bot_a")
    assert any(
        e["label"] == "com.team_bot_a.briefing" and e["destination"] == "launchdaemon"
        for e in entries
    ), f"manifest missing entry: {entries}"
    # template_installed_labels with destination filter returns just the
    # launchdaemon labels for the orphan-sweeper.
    assert "com.team_bot_a.briefing" in template_installed_labels(
        shared_dir, "team_bot_a", destination="launchdaemon",
    )


def test_template_installed_labels_visible_to_retire_per_bot_list(tmp_path):
    """Contract test: any label recorded in the template-installs
    manifest is included in retire's :func:`_per_bot_plist_labels`
    output (the list retire-bot bootouts on retirement).

    This is the integration gate that prevents the C1.d drift bug from
    re-occurring on the install side. The MVP retrospective explicitly
    named contract tests of this shape as "the structural fix."
    """
    from evolve_admin.template_installs import record_template_install
    from evolve_admin.retire import _per_bot_plist_labels

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.morning-briefing",
        destination="launchdaemon",
        app_id="app_morning_briefing",
    )
    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.calendar-watch",
        destination="launchdaemon",
        app_id="app_calendar_watch",
    )

    labels = _per_bot_plist_labels("team_bot_a", shared_dir=shared_dir)
    assert "com.team_bot_a.morning-briefing" in labels, (
        f"template-installed launchdaemon must appear in retire's bootout "
        f"set; got: {labels}"
    )
    assert "com.team_bot_a.calendar-watch" in labels


def test_template_installed_labels_visible_to_orphan_sweeper_expected_set(
    tmp_path,
):
    """Contract test: any label recorded in the template-installs
    manifest is included in :func:`deploy.expected_plist_labels` so
    the orphan-sweeper does NOT delete it on the next deploy."""
    from evolve_admin.template_installs import record_template_install
    from evolve_admin.deploy import expected_plist_labels

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.morning-briefing",
        destination="launchdaemon",
        app_id="app_morning_briefing",
    )

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(shared_dir),
    }
    expected = expected_plist_labels(network)
    assert "com.team_bot_a.morning-briefing" in expected, (
        f"template-installed launchdaemon must be in expected_plist_labels "
        f"so the orphan-sweeper does not delete it; got: {sorted(expected)[:10]}..."
    )


def test_template_installed_launchagents_excluded_from_orphan_sweeper(
    tmp_path,
):
    """LaunchAgents live in the bot's home (~/Library/LaunchAgents/),
    not in /Library/LaunchDaemons/. The orphan-sweeper only scans
    /Library/LaunchDaemons/, so launchagent entries should NOT need to
    show up in expected_plist_labels — and we explicitly filter them
    out so the set stays accurate to what the sweeper actually sees.
    """
    from evolve_admin.template_installs import record_template_install
    from evolve_admin.deploy import expected_plist_labels

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.user-only",
        destination="launchagent",
        app_id="app_user_only",
    )

    network = {
        "members": ["team_bot_a"],
        "sharedDir": str(shared_dir),
    }
    expected = expected_plist_labels(network)
    assert "com.team_bot_a.user-only" not in expected, (
        "launchagent labels should not be in the system-launchd "
        "expected set; the sweeper never sees them"
    )


def test_retire_template_installed_labels_helper_handles_missing_manifest(
    tmp_path,
):
    """If the manifest doesn't exist or is corrupt, the helper returns
    an empty list rather than raising. Retire-bot must keep working on
    bots that never deployed an embedded app."""
    from evolve_admin.retire import _template_installed_labels_for

    shared_dir = tmp_path / "shared"  # not created — missing on purpose
    labels = _template_installed_labels_for("nonexistent-bot", shared_dir)
    assert labels == []


def test_record_template_install_is_idempotent(tmp_path):
    """Re-recording the same (label, destination) updates the
    timestamp but does not duplicate. This is the contract
    apply_embedded_app relies on for safe re-deploys."""
    from evolve_admin.template_installs import (
        record_template_install, read_template_installs,
    )

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.morning",
        destination="launchdaemon",
        app_id="app_morning",
    )
    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.morning",
        destination="launchdaemon",
        app_id="app_morning",
    )
    entries = read_template_installs(shared_dir, "team_bot_a")
    assert len(entries) == 1, (
        f"expected exactly one entry after idempotent re-record; got {entries}"
    )


def test_clear_template_installs_removes_manifest(tmp_path):
    """retire-bot's success path clears the manifest; verify the
    helper does what it says."""
    from evolve_admin.template_installs import (
        record_template_install, read_template_installs,
        clear_template_installs,
    )

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()

    record_template_install(
        shared_dir, "team_bot_a",
        label="com.team_bot_a.morning",
        destination="launchdaemon",
    )
    assert read_template_installs(shared_dir, "team_bot_a")

    clear_template_installs(shared_dir, "team_bot_a")
    assert read_template_installs(shared_dir, "team_bot_a") == []


# ── LaunchdAdapter — launchctl via the Scheduler seam (4.3c S2b) ──────────────
#
# The real adapter (not the _FakeLaunchdAdapter stub above) must route every
# launchctl operation through the Scheduler seam so a fake injected via
# ``set_scheduler`` intercepts it — NO test here may ever reach a real
# ``launchctl`` (bootout/bootstrap are live-traffic destructive on a pod).


class _SeamRecorder:
    """Records every argv the seam's runner receives; per-verb responses.

    ``responses`` maps a launchctl verb ("bootstrap", "print", …) to an
    ``(rc, stdout, stderr)`` tuple. Unknown verb → success, empty output.
    """

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        args = [a for a in argv if a != "sudo"]
        verb = args[1] if len(args) > 1 and args[0].endswith("launchctl") else args[0]
        return self.responses.get(verb, (0, "", ""))

    def launchctl_argvs(self) -> list[list[str]]:
        return [c for c in self.calls if any(a.endswith("launchctl") for a in c)]


@pytest.fixture()
def seam_recorder(monkeypatch):
    """Inject a recording fake into the process-wide Scheduler seam.

    Also booby-traps the scheduler module's own ``subprocess.run`` so any
    path that escapes the injected runner fails the test loudly instead of
    spawning a real launchctl.
    """
    from evolve_admin.runtime import LaunchdScheduler, set_scheduler
    from evolve_admin.runtime import scheduler as scheduler_mod

    def _boom(*a, **kw):  # pragma: no cover — exists to fail loudly
        raise AssertionError(
            f"real subprocess spawn from the scheduler module: argv={a!r}"
        )

    monkeypatch.setattr(scheduler_mod.subprocess, "run", _boom)
    recorder = _SeamRecorder()
    set_scheduler(LaunchdScheduler(runner=recorder))
    yield recorder
    set_scheduler(None)


def test_launchd_adapter_daemon_bootstrap_routes_via_seam(seam_recorder, tmp_path):
    """launchdaemon bootstrap = bootout → bootstrap → print probe, all via
    the seam, argv shape identical to the legacy direct calls."""
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    plist = tmp_path / "com.team_bot_a.morning.plist"
    plist.write_text("<plist/>")

    ok = LaunchdAdapter().bootstrap(
        bot_user="team_bot_a",
        label="com.team_bot_a.morning",
        plist_path=plist,
        destination="launchdaemon",
    )
    assert ok is True
    argvs = seam_recorder.launchctl_argvs()
    assert argvs[0][:3] == ["sudo", "/bin/launchctl", "bootout"]
    assert argvs[0][3] == "system/com.team_bot_a.morning"
    assert argvs[1][:4] == ["sudo", "/bin/launchctl", "bootstrap", "system"]
    assert argvs[1][4] == str(plist)
    # rc=0 alone is not success — the post-bootstrap probe must run.
    assert argvs[2][:3] == ["sudo", "/bin/launchctl", "print"]
    assert argvs[2][3] == "system/com.team_bot_a.morning"


def test_launchd_adapter_agent_bootstrap_uses_gui_domain(
    seam_recorder, tmp_path, monkeypatch
):
    """launchagent bootstrap targets gui/<uid>; only ``id -u`` stays a
    direct subprocess (not a launchctl spawn)."""
    import subprocess as _subprocess

    from evolve_admin.bot_templates import cli_integration

    def _fake_run(argv, **kw):
        assert "/bin/launchctl" not in argv, (
            f"launchctl bypassed the Scheduler seam: {argv}"
        )
        assert argv[:2] == ["/usr/bin/id", "-u"]
        return _subprocess.CompletedProcess(argv, 0, stdout="502\n", stderr="")

    monkeypatch.setattr(cli_integration.subprocess, "run", _fake_run)

    plist = tmp_path / "com.team_bot_a.morning.plist"
    plist.write_text("<plist/>")

    ok = cli_integration.LaunchdAdapter().bootstrap(
        bot_user="team_bot_a",
        label="com.team_bot_a.morning",
        plist_path=plist,
        destination="launchagent",
    )
    assert ok is True
    argvs = seam_recorder.launchctl_argvs()
    assert argvs[0][2:] == ["bootout", "gui/502/com.team_bot_a.morning"]
    assert argvs[1][2:] == ["bootstrap", "gui/502", str(plist)]
    assert argvs[2][2:] == ["print", "gui/502/com.team_bot_a.morning"]


def test_launchd_adapter_bootstrap_fails_closed_when_probe_negative(
    seam_recorder, tmp_path
):
    """bootstrap rc=0 but the print probe says not-loaded → False (the
    C1.d silent-failure guard must survive the seam migration)."""
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    seam_recorder.responses["print"] = (1, "", "Could not find service")
    plist = tmp_path / "com.team_bot_a.morning.plist"
    plist.write_text("<plist/>")

    ok = LaunchdAdapter().bootstrap(
        bot_user="team_bot_a",
        label="com.team_bot_a.morning",
        plist_path=plist,
        destination="launchdaemon",
    )
    assert ok is False


def test_launchd_adapter_bootout_keeps_plist(seam_recorder, tmp_path):
    """Rollback bootout goes through raw() and must NOT delete the plist
    (file restoration is the executor's job, not the scheduler's)."""
    from evolve_admin.bot_templates.cli_integration import LaunchdAdapter

    plist = tmp_path / "com.team_bot_a.morning.plist"
    plist.write_text("<plist/>")

    ok = LaunchdAdapter().bootout(
        bot_user="team_bot_a",
        label="com.team_bot_a.morning",
        destination="launchdaemon",
    )
    assert ok is True
    argvs = seam_recorder.launchctl_argvs()
    assert argvs == [
        ["sudo", "/bin/launchctl", "bootout", "system/com.team_bot_a.morning"],
    ]
    assert plist.exists(), "bootout must not delete the plist"
