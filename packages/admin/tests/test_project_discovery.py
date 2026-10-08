"""Repository discovery (D-AX1): every rule fires on its fixture and nowhere
else, no secret value ever reaches the output, docs raise / introduce
confidence, unknown providers are listed, and the second repository shape
(design §11) yields its two findings."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from evolve_admin import project_discovery as pd
from evolve_admin import project_discovery_rules as R
from evolve_admin.connection_capabilities import DISCOVERY_CAPABILITIES, SERVICE_CATALOG

FIX = Path(__file__).parent / "fixtures" / "project_discovery"
FAKE_SECRET = "zz_fake_FAKEFAKEFAKEFAKE0123456789abcdef"


def _svc(m: pd.ServicesManifest) -> dict[str, pd.ServiceEntry]:
    return {s.service: s for s in m.services}


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ── the rule table is consistent with the one vocabulary ────────────────────


def test_rules_use_the_capability_map_vocabulary() -> None:
    rules = [*R.DEP_RULES, *R.ENV_RULES, *R.FILE_RULES, *R.CI_RULES]
    for rule in rules:
        assert rule.service in SERVICE_CATALOG, rule
        assert set(rule.capabilities or ()) <= set(DISCOVERY_CAPABILITIES), rule
    for info in SERVICE_CATALOG.values():
        assert set(info["capabilities"]) <= set(DISCOVERY_CAPABILITIES)
    # the brief's own capability list is present
    assert {"deploy", "database", "email", "dns", "auth", "embeddings", "payments", "ci"} <= set(DISCOVERY_CAPABILITIES)


# ── every rule fires on its own minimal input, and only there ───────────────


def _only(m: pd.ServicesManifest, service: str) -> None:
    assert service in _svc(m), (service, [s.service for s in m.services])


@pytest.mark.parametrize("rule", R.DEP_RULES, ids=lambda r: f"dep:{r.service}:{r.example}")
def test_dep_rules_fire(rule: R.DepRule, tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"name": "x", "dependencies": {rule.example: "1"}}))
    _only(pd.discover(tmp_path), rule.service)
    reqs = tmp_path / "py"
    _write(reqs, "requirements.txt", f"{rule.example.lstrip('@').replace('/', '-')}>=1\n" if not rule.example.startswith("@") else "")
    if not rule.example.startswith("@"):
        _only(pd.discover(reqs), rule.service)
        _write(tmp_path / "toml", "pyproject.toml", f'[project]\nname = "x"\ndependencies = ["{rule.example}>=1"]\n')
        _only(pd.discover(tmp_path / "toml"), rule.service)
        _write(tmp_path / "gem", "Gemfile", f'gem "{rule.example}"\n')
        _only(pd.discover(tmp_path / "gem"), rule.service)
    if rule.go_example:
        _write(tmp_path / "go", "go.mod", f"module m\n\nrequire {rule.go_example} v1.2.3\n")
        _only(pd.discover(tmp_path / "go"), rule.service)


@pytest.mark.parametrize("rule", R.ENV_RULES, ids=lambda r: f"env:{r.service}")
def test_env_rules_fire(rule: R.EnvRule, tmp_path: Path) -> None:
    _write(tmp_path, ".env.example", f"{rule.example}=\n")
    m = pd.discover(tmp_path)
    assert rule.service in _svc(m)
    assert m.get(rule.service).confidence == R.NAMED  # type: ignore[union-attr]
    assert not [s for s in m.services if s.service.startswith("unknown:")]


@pytest.mark.parametrize("rule", R.FILE_RULES, ids=lambda r: f"file:{r.service}:{r.example_path}")
def test_file_rules_fire(rule: R.FileRule, tmp_path: Path) -> None:
    _write(tmp_path, rule.example_path, rule.example_content)
    m = pd.discover(tmp_path)
    assert rule.service in _svc(m)
    assert m.get(rule.service).confidence == R.CERTAIN  # type: ignore[union-attr]
    if rule.line_required:  # ...and a file with the right name but no matching line does not
        other = tmp_path / "neg"
        _write(other, rule.example_path, "# nothing relevant\n")
        assert rule.service not in _svc(pd.discover(other))


@pytest.mark.parametrize("rule", R.CI_RULES, ids=lambda r: f"ci:{r.service}")
def test_ci_rules_fire(rule: R.CiRule, tmp_path: Path) -> None:
    _write(tmp_path, ".github/workflows/x.yml", f"jobs:\n  a:\n    steps:\n      - {rule.example}\n")
    m = pd.discover(tmp_path)
    assert rule.service in _svc(m)
    assert m.get("github") is not None  # the pipeline itself


def test_a_repo_with_no_evidence_names_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "print('hi')\n")
    _write(tmp_path, "package.json", '{"name": "resend-clone", "scripts": {"vercel-build": "x"}}')
    _write(tmp_path, ".env.example", "NODE_ENV=\nPORT=\nAPP_URL=\nFEATURE_X_ENABLED=\n")
    m = pd.discover(tmp_path)
    assert m.services == [] and m.findings == []


def test_unrelated_fixtures_do_not_leak_across() -> None:
    got = _svc(pd.discover(FIX / "static-site-dns"))
    assert set(got) >= {"netlify", "dnscontrol", "dns-zone"}
    assert not {"supabase", "resend", "openai", "vercel"} & set(got)


# ── the fixtures ─────────────────────────────────────────────────────────────


def test_worked_case_shape() -> None:
    m = pd.discover(FIX / "storefront-next")
    got = _svc(m)
    assert got["vercel"].confidence == R.CONFIRMED and "deploy" in got["vercel"].capabilities
    assert {"database", "auth"} <= set(got["supabase"].capabilities)
    assert got["resend"].capabilities == ["email"]
    assert "embeddings" in got["openai"].capabilities
    assert "ci" in got["github"].capabilities
    # DATABASE_URL folded into the named host rather than shown as a second database
    assert "sql-database" not in got
    assert any(e.pattern == "DATABASE_URL" for e in got["supabase"].evidence)
    # every entry carries at least one evidence line with a path and a line number
    for s in m.services:
        assert s.evidence and all(e.path for e in s.evidence)
    # deploy-db.yml runs `supabase db push`: no migrations finding (only the unrecognised env name)
    assert [f.kind for f in m.findings] == [pd.UNRECOGNISED_ENV]
    assert "SUPABASE_PROJECT_REF" in json.dumps(m.to_json())
    assert "value not read" in got["supabase"].details["project_ref"]


def test_docs_raise_confirm_and_introduce_unverified() -> None:
    got = _svc(pd.discover(FIX / "storefront-next"))
    assert got["supabase"].confidence == R.CONFIRMED and got["resend"].confidence == R.CONFIRMED
    assert got["openai"].confidence == R.NAMED or got["openai"].confidence == R.LIKELY  # docs never named it
    assert got["stripe"].confidence == R.UNVERIFIED
    assert got["stripe"].evidence[0].path == "docs/04-tech-stack.md"


def test_unrecognised_env_names_are_a_finding_not_a_row() -> None:
    m = pd.discover(FIX / "storefront-next")
    assert not [s for s in m.services if s.service.startswith("unknown:")]
    f = next(f for f in m.findings if f.kind == pd.UNRECOGNISED_ENV)
    assert f.message == "unrecognised env names: LEDGERLY_API_KEY"
    assert f.evidence[0].pattern == "LEDGERLY_API_KEY"


def test_python_service_with_a_queue() -> None:
    got = _svc(pd.discover(FIX / "queue-worker-py"))
    assert got["docker"].capabilities == [] and got["docker"].local_tooling
    assert "queue" in got["redis"].capabilities and got["redis"].confidence == R.CERTAIN
    assert got["celery"].capabilities == ["queue"]
    assert "monitoring" in got["error-tracking"].capabilities
    assert "payments" in got["stripe"].capabilities      # go.mod
    assert "email" in got["resend"].capabilities         # Gemfile
    assert "supabase" not in got


def test_static_site_with_dns() -> None:
    got = _svc(pd.discover(FIX / "static-site-dns"))
    assert got["netlify"].confidence == R.CONFIRMED
    assert got["dnscontrol"].capabilities == ["dns"] and got["dns-zone"].capabilities == ["dns"]


def test_second_shape_names_deploy_database_ci_and_two_findings() -> None:
    m = pd.discover(FIX / "second-shape")
    got = _svc(m)
    assert {"vercel", "supabase", "github"} <= set(got)
    assert "deploy" in got["vercel"].capabilities and got["vercel"].details["project"] == "example-scope/dice-game"
    assert any(e.path == ".vercelignore" for e in got["vercel"].evidence)
    assert "database" in got["supabase"].capabilities
    assert got["supabase"].details["project_ref"] == "unknown — the wizard asks"
    assert any(e.pattern == "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY" for e in got["supabase"].evidence)
    assert "ci" in got["github"].capabilities
    assert not [s for s in got if "email" in got[s].capabilities or {"llm", "embeddings"} & set(got[s].capabilities)]
    kinds = {f.kind for f in m.findings}
    assert kinds == {pd.MIGRATIONS_NO_PIPELINE, pd.SHARED_ACCOUNT}
    shared = next(f for f in m.findings if f.kind == pd.SHARED_ACCOUNT)
    assert shared.message == "shared provider account (see D-AX12)"
    assert shared.evidence[0].path == "docs/SETUP-DECISIONS.md"
    assert "migrations directory with no pipeline" in m.render_table()


# ── secrets ─────────────────────────────────────────────────────────────────


def _with_stray_env(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIX / "storefront-next", repo)
    (repo / ".env").write_text(f"STRIPE_SECRET_KEY={FAKE_SECRET}\nRESEND_API_KEY=re_FAKEFAKEFAKE0000\n")
    (repo / ".env.production").write_text(f"OPENAI_API_KEY={FAKE_SECRET}\n")
    # values in the example file itself must not survive either
    with (repo / ".env.example").open("a") as f:
        f.write(f"PLANTED_API_KEY={FAKE_SECRET}\n")
    (repo / "docs" / "04-tech-stack.md").write_text(
        f"Resend key is api_key={FAKE_SECRET} (do not commit). Uses Stripe.\n")
    return repo


def test_stray_env_is_reported_and_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _with_stray_env(tmp_path)
    opened: list[str] = []
    real = pd._read_text
    monkeypatch.setattr(pd, "_read_text", lambda p: (opened.append(p.name), real(p))[1])
    m = pd.discover(repo)
    assert ".env" not in opened and ".env.production" not in opened
    finds = [f for f in m.findings if f.kind == pd.SECRETS_FILE]
    assert {f.evidence[0].path for f in finds} == {".env", ".env.production"}
    assert all("not opened" in f.message for f in finds)
    # contents did not drive any rule: stripe/openai secrets only in the real files
    assert "stripe" not in _svc(m) or _svc(m)["stripe"].confidence == R.UNVERIFIED


def test_read_text_refuses_a_real_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("A=b\n")
    with pytest.raises(PermissionError):
        pd._read_text(tmp_path / ".env.local")
    (tmp_path / ".env.local.example").write_text("A=\n")
    assert pd._read_text(tmp_path / ".env.local.example") == ["A="]


def test_no_secret_value_reaches_any_output(tmp_path: Path) -> None:
    repo = _with_stray_env(tmp_path)
    m = pd.discover(repo)
    blob = json.dumps(m.to_json()) + m.render_table() + pd.run_cli(str(repo), True) + pd.run_cli(str(repo), False)
    for needle in (FAKE_SECRET, "FAKEFAKEFAKE", "zz_fake"):
        assert needle not in blob


def test_symlinked_env_example_is_not_a_way_in(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("RESEND_API_KEY=abc\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env.example").symlink_to(outside)
    assert pd.discover(repo).services == []


def test_redact_strips_secret_shapes() -> None:
    assert FAKE_SECRET not in pd.redact(f"token = {FAKE_SECRET}")
    assert "hunter2" not in pd.redact("postgres://user:hunter2@host/db")
    assert len(pd.redact("x" * 500)) <= pd.MAX_LINE


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_table_and_json() -> None:
    from evolve_admin.board_import import project_group

    runner = CliRunner()
    r = runner.invoke(project_group, ["discover", str(FIX / "storefront-next")])
    assert r.exit_code == 0, r.output
    for word in ("SERVICE", "CAPABILITIES", "EVIDENCE", "CONFIDENCE", "Vercel", "Supabase", "Resend", "OpenAI", "GitHub"):
        assert word in r.output
    j = runner.invoke(project_group, ["discover", "--json", str(FIX / "storefront-next")])
    assert json.loads(j.output)["services"]
    bad = runner.invoke(project_group, ["discover", "/nonexistent/nowhere"])
    assert bad.exit_code != 0 and "not a directory" in bad.output
    assert runner.invoke(project_group, ["discover", "--", "-oProxyCommand=x"]).exit_code != 0


def test_discover_is_registered_on_the_main_cli() -> None:
    from evolve_admin.cli import main

    assert "discover" in main.commands["project"].commands  # type: ignore[attr-defined]


def test_setup_doc_phrasing_variants_raise_the_shared_account_finding(tmp_path: Path) -> None:
    _write(tmp_path, "SETUP-NOTES.md", "A dedicated project was created under the existing `other-app` Supabase organization.\n")
    _write(tmp_path, "README.md", "This shares the Supabase org with a sibling.\n")  # not a setup doc: no finding
    finds = [f for f in pd.discover(tmp_path).findings if f.kind == pd.SHARED_ACCOUNT]
    assert [f.evidence[0].path for f in finds] == ["SETUP-NOTES.md"]


def test_agent_workspaces_are_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, ".claude/worktrees/x/package.json", '{"dependencies": {"resend": "1"}}')
    assert pd.discover(tmp_path).services == []


def test_redaction_count_is_reported(tmp_path: Path) -> None:
    repo = _with_stray_env(tmp_path)
    m = pd.discover(repo)
    assert m.redactions >= 1
    assert f"{m.redactions} secret- or id-shaped value(s) redacted" in m.render_table()
    assert m.to_json()["redactions"] == m.redactions
    clean = pd.ServicesManifest(repo="x")
    assert "redacted" not in clean.render_table()


# ── real-shape-one: the first real run's tree shape (finding 2026-10-04) ────


def _real() -> pd.ServicesManifest:
    return pd.discover(FIX / "real-shape-one")


def test_real_shape_tiers() -> None:
    got = _svc(_real())
    tiers = {s: e.confidence for s, e in got.items()}
    assert [s for s, t in tiers.items() if t == R.CONFIRMED].__len__() == 4
    assert {s for s, t in tiers.items() if t == R.CONFIRMED} == {"github", "resend", "supabase", "vercel"}
    assert {s: tiers[s] for s in ("anthropic", "voyage")} == {"anthropic": R.LIKELY, "voyage": R.NAMED}
    assert tiers["cloudflare"] == R.UNVERIFIED
    assert tiers["error-tracking"] == R.PLANNED
    assert tiers["clerk"] == R.ALTERNATIVE
    assert tiers["docker"] == R.UNVERIFIED and got["docker"].local_tooling and got["docker"].capabilities == []
    assert set(tiers) == {"github", "resend", "supabase", "vercel", "anthropic", "voyage", "cloudflare",
                          "error-tracking", "clerk", "docker"}


def test_planned_and_alternative_sort_after_present_services() -> None:
    order = [s.service for s in _real().services]
    assert order.index("cloudflare") < order.index("error-tracking") < order.index("clerk")
    assert order.index("voyage") < order.index("cloudflare")


def test_real_shape_unrecognised_names_and_local_tooling_render() -> None:
    m = _real()
    assert [f.message for f in m.findings if f.kind == pd.UNRECOGNISED_ENV] == ["unrecognised env names: VIEW_AS_COOKIE_SECRET"]
    assert not [s for s in m.services if s.service.startswith("unknown:")]
    table = m.render_table()
    assert "Local tooling" in table and table.index("Local tooling") < table.index("Docker")
    assert table.index("Docker") > table.index("Clerk")  # not in the main table
    assert m.to_json()["services"][[s.service for s in m.services].index("docker")]["local_tooling"] is True


def test_real_shape_no_identifier_in_any_excerpt() -> None:
    m = _real()
    blob = json.dumps(m.to_json()) + m.render_table()
    for needle in ("abcdefghij0123456789", "123e4567-e89b-12d3-a456-426614174000", "proj_Ab12Cd34Ef56"):
        assert needle not in blob
    assert m.redactions == 3
    assert "3 secret- or id-shaped value(s) redacted" in m.render_table()
    assert "value not read" in _svc(m)["supabase"].details["project_ref"]


def test_docs_line_must_name_the_provider() -> None:
    got = _svc(_real())
    github = [e for e in got["github"].evidence if e.path == "README.md"]
    assert [e.line for e in github] == [8]  # the Vitest/Playwright line names no provider


def test_id_shapes_are_masked_and_counted() -> None:
    for text in ("ref: abcdef123456", "project ref `abcdef123456`", "uuid 123e4567-e89b-12d3-a456-426614174000",
                 "proj_AbC123", "id abcdefghijklmnopqrst"):
        out, n = pd.redact_counted(text)
        assert n >= 1 and ("[id]" in out or "[redacted]" in out), text
    assert pd.redact_counted("ref the docs; see the README")[1] == 0


def test_alternative_needs_two_providers_on_a_list_row_and_no_other_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "- Clerk\n| Auth0 | auth | Rejected — Clerk instead |\n")
    got = _svc(pd.discover(tmp_path))
    assert got["clerk"].confidence == R.UNVERIFIED  # a plain single-provider list item is a mention
    assert got["auth0"].confidence == R.ALTERNATIVE or got["auth0"].confidence == R.UNVERIFIED
    _write(tmp_path, "README.md", "| Clerk | auth | Rejected for Auth0 |\n")
    _write(tmp_path, "package.json", '{"dependencies": {"@clerk/nextjs": "1"}}')
    assert _svc(pd.discover(tmp_path))["clerk"].confidence == R.CONFIRMED  # non-docs evidence wins


def test_planned_phrases(tmp_path: Path) -> None:
    for line in ("Stripe is planned.", "Stripe later.", "TODO: Stripe", "Add Stripe in Phase 3."):
        _write(tmp_path, "README.md", line + "\n")
        assert _svc(pd.discover(tmp_path))["stripe"].confidence == R.PLANNED, line
    _write(tmp_path, "README.md", "Stripe is planned.\nStripe takes payments.\n")
    assert _svc(pd.discover(tmp_path))["stripe"].confidence == R.UNVERIFIED
