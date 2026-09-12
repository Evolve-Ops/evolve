"""Tests for app_permission_review's first-pass static checks (review.py).

Each test constructs a synthetic manifest + matching workspace files
in tmp_path, runs ``find_per_app_candidates``, and asserts the
expected ``Finding`` shape.
"""
from __future__ import annotations

from pathlib import Path

from generators.app_permission_review.review import (
    KIND_EGRESS_MISSING_DECLARATION,
    KIND_EGRESS_OVERKILL_WILDCARD,
    KIND_ENV_UNUSED,
    KIND_EXEC_MISSING_DECLARATION,
    KIND_EXEC_OVERKILL_WILDCARD,
    KIND_EXEC_UNUSED,
    KIND_FS_READ_UNUSED,
    KIND_FS_WRITE_UNUSED,
    KIND_NETWORK_EGRESS_UNUSED,
    Finding,
    _affirmed_set,
    _has_wildcard,
    _is_plausible_egress_host,
    _wildcard_to_regex,
    find_per_app_candidates,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a synthetic workspace with the given files (rel path → body)."""
    ws = tmp_path / "workspace"
    for rel, body in files.items():
        full = ws / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body)
    ws.mkdir(exist_ok=True)
    return ws


def _by_kind(findings, kind: str):
    return [f for f in findings if f.kind == kind]


# ── Helper unit tests ────────────────────────────────────────────────────────


def test_has_wildcard():
    assert _has_wildcard("scripts/*.py") is True
    assert _has_wildcard("scripts/foo?.py") is True
    assert _has_wildcard("scripts/foo.py") is False
    assert _has_wildcard("") is False


def test_wildcard_to_regex_basic_match():
    rx = _wildcard_to_regex("*.anthropic.com")
    assert rx.match("api.anthropic.com")
    assert rx.match("console.anthropic.com")
    assert not rx.match("api.example.com")


def test_wildcard_to_regex_question_mark():
    rx = _wildcard_to_regex("scripts/foo?.py")
    assert rx.match("scripts/foo1.py")
    assert rx.match("scripts/fooA.py")
    assert not rx.match("scripts/foo12.py")  # ? matches single char only


def test_affirmed_set_reads_permissions_underscore_affirmed():
    manifest = {
        "permissions": {
            "_affirmed": ["permission_exec_unused:exec:scripts/foo.py"],
        },
    }
    assert _affirmed_set(manifest) == {
        "permission_exec_unused:exec:scripts/foo.py",
    }


def test_affirmed_set_handles_missing():
    assert _affirmed_set({}) == set()
    assert _affirmed_set({"permissions": None}) == set()
    assert _affirmed_set({"permissions": {"_affirmed": None}}) == set()


# ── KIND_EXEC_UNUSED ─────────────────────────────────────────────────────────


def test_exec_unused_when_file_missing_and_no_grep_match(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/real.py": "# real script that doesn't mention the missing one",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/real.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/ghost.py"],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    unused = _by_kind(findings, KIND_EXEC_UNUSED)
    assert len(unused) == 1
    assert unused[0].entry_value == "scripts/ghost.py"
    assert unused[0].severity == "warn"


def test_exec_not_unused_when_file_exists(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/real.py": "# real",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/real.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/real.py"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EXEC_UNUSED) == []


def test_exec_not_unused_when_basename_grep_matched(tmp_path: Path):
    """Indirect invocation — script B subprocess-runs script A. A is
    valid even if not in files[]."""
    ws = _make_workspace(tmp_path, {
        "scripts/main.py": "subprocess.run(['python3', 'utils/helper.py'])",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/main.py", "layer": "script"}],
        # helper.py doesn't exist on disk, but main.py grep-references it
        "permissions": {"exec": ["utils/helper.py"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EXEC_UNUSED) == []


def test_exec_unused_skipped_when_affirmed(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/real.py": "# real"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/real.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/ghost.py"],
            "_affirmed": [
                "permission_exec_unused:exec:scripts/ghost.py",
            ],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EXEC_UNUSED) == []


def test_exec_with_interpreter_prefix_resolves_to_path(tmp_path: Path):
    """Entries like 'python3 scripts/foo.py' should check existence of
    scripts/foo.py, not the whole entry string."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/foo.py", "layer": "script"}],
        "permissions": {"exec": ["python3 scripts/foo.py"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    # File exists, so no exec_unused — only the missing-declaration check
    # might fire (interpreter-form doesn't match path-form for sufficiency)
    assert _by_kind(findings, KIND_EXEC_UNUSED) == []


# ── KIND_FS_*_UNUSED / KIND_NETWORK_EGRESS_UNUSED / KIND_ENV_UNUSED ──────────


def test_fs_read_unused_when_no_script_references_path(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/run.py": "print('hi')",  # doesn't mention any path
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"fs_read": ["/Users/Shared/evolve/proposals/"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    unused = _by_kind(findings, KIND_FS_READ_UNUSED)
    assert len(unused) == 1
    assert unused[0].entry_value == "/Users/Shared/evolve/proposals/"
    assert unused[0].severity == "info"  # advisory


def test_fs_read_kept_when_grep_matched(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/run.py": "open('/Users/Shared/evolve/proposals/x.json')",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"fs_read": ["/Users/Shared/evolve/proposals/"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_FS_READ_UNUSED) == []


def test_fs_write_unused_emits_kind(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/run.py": "pass"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"fs_write": ["/Users/Shared/evolve/output/"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    unused = _by_kind(findings, KIND_FS_WRITE_UNUSED)
    assert len(unused) == 1


def test_network_egress_unused(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/run.py": "pass"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"network_egress": ["api.example.com"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    unused = _by_kind(findings, KIND_NETWORK_EGRESS_UNUSED)
    assert len(unused) == 1


def test_env_unused(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/run.py": "pass"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"env": ["NEVER_USED_VAR"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    unused = _by_kind(findings, KIND_ENV_UNUSED)
    assert len(unused) == 1


def test_env_kept_when_referenced(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/run.py": "import os; key = os.environ['ANTHROPIC_API_KEY']",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"env": ["ANTHROPIC_API_KEY"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_ENV_UNUSED) == []


def test_network_egress_wildcard_finds_literal_substring(tmp_path: Path):
    """*.anthropic.com should grep against `.anthropic.com` literal."""
    ws = _make_workspace(tmp_path, {
        "scripts/run.py": "url = 'https://api.anthropic.com/v1/messages'",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {"network_egress": ["*.anthropic.com"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_NETWORK_EGRESS_UNUSED) == []


# ── KIND_EXEC_MISSING_DECLARATION ────────────────────────────────────────────


def test_exec_missing_declaration_for_script_not_in_exec_list(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/foo.py", "layer": "script"}],
        # permissions block exists but doesn't declare foo.py
        "permissions": {"fs_read": ["/Users/Shared/x"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    missing = _by_kind(findings, KIND_EXEC_MISSING_DECLARATION)
    assert len(missing) == 1
    assert missing[0].entry_value == "scripts/foo.py"
    assert missing[0].severity == "warn"


def test_exec_missing_skipped_when_wildcard_matches(tmp_path: Path):
    """A wildcard exec entry that matches the script path covers it."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/foo.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/*.py"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EXEC_MISSING_DECLARATION) == []


def test_exec_missing_declaration_v7_arc_realized_files(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"tools/v7.py": "# v7"})
    manifest = {
        "instance_id": "i-v7",
        "manifest_shape": "v7-arc",
        "schema_version": 14,
        "files": [],
        "realized_files": [
            {"path": "tools/v7.py", "marker_state": "OWNED",
             "file_id": "f-1", "logical_name": "v7"},
        ],
        "permissions": {"exec": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    missing = _by_kind(findings, KIND_EXEC_MISSING_DECLARATION)
    assert len(missing) == 1
    assert missing[0].entry_value == "tools/v7.py"


# ── KIND_EGRESS_MISSING_DECLARATION ──────────────────────────────────────────


def test_egress_missing_declaration_for_hardcoded_host(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/api.py": "import requests\nresp = requests.get('https://api.openrouter.ai/v1/x')",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/api.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/api.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    missing = _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION)
    assert len(missing) == 1
    assert missing[0].entry_value == "api.openrouter.ai"


def test_egress_missing_skipped_when_declared(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/api.py": "url = 'https://api.openrouter.ai/v1'",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/api.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/api.py"],
            "network_egress": ["api.openrouter.ai"],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION) == []


def test_egress_missing_covered_by_wildcard(tmp_path: Path):
    ws = _make_workspace(tmp_path, {
        "scripts/api.py": "url = 'https://api.anthropic.com'",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/api.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/api.py"],
            "network_egress": ["*.anthropic.com"],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION) == []


# ── Egress host-plausibility gate (false-positive suppression) ───────────────
#
# Each of these reproduces a false positive observed on the live pod, where
# the host regexes grepped a non-egress token out of a real script body.


def test_is_plausible_egress_host_keeps_real_hosts():
    for host in (
        "slack.com", "api.github.com", "api.anthropic.com",
        "api.prod.whoop.com", "api.openrouter.ai", "example.co.uk",
        "hooks.slack.com",
    ):
        assert _is_plausible_egress_host(host) is True, host


def test_is_plausible_egress_host_rejects_attribute_access():
    # `api.authenticate` etc. are method calls grepped by _API_HOST_REGEX;
    # the final label is an English word, not a TLD.
    for token in ("api.authenticate", "api.get", "api.post", "api.session"):
        assert _is_plausible_egress_host(token) is False, token


def test_is_plausible_egress_host_rejects_loopback_and_local():
    for token in (
        "127.0.0.1", "localhost", "0.0.0.0", "::1",
        "192.168.1.10", "10.0.0.5", "172.16.0.1", "169.254.0.1",
    ):
        assert _is_plausible_egress_host(token) is False, token


def test_is_plausible_egress_host_rejects_xml_namespaces():
    for token in (
        "schemas.openxmlformats.org",
        "schemas.openxmlformats.org",  # subhost-suffix form
        "www.w3.org", "purl.org", "schema.org",
    ):
        assert _is_plausible_egress_host(token) is False, token


def test_is_plausible_egress_host_rejects_malformed():
    for token in ("...", "", "api", "localhost.", "a b.com"):
        assert _is_plausible_egress_host(token) is False, token


def test_egress_missing_skips_attribute_access_false_positive(tmp_path: Path):
    """`if not api.authenticate():` must NOT yield an egress finding."""
    ws = _make_workspace(tmp_path, {
        "scripts/g.py": (
            "api = build_client()\n"
            "if not api.authenticate():\n"
            "    raise SystemExit(1)\n"
        ),
    })
    manifest = {
        "id": "i-app", "name": "Google Services Integration",
        "files": [{"path": "scripts/g.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/g.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION) == []


def test_egress_missing_skips_gtld_method_name_false_positive(tmp_path: Path):
    """`api.run(...)` / `api.app` — method names that are also gTLDs — must
    NOT yield egress findings. The bare api-form requires >=2 dots, which a
    genuine api host (`api.github.com`) has and a method call does not."""
    ws = _make_workspace(tmp_path, {
        "scripts/r.py": (
            "result = api.run(task)\n"
            "handle = api.app.config\n"
            "client = api.dev_session()\n"
        ),
    })
    manifest = {
        "id": "i-app", "name": "Runner",
        "files": [{"path": "scripts/r.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/r.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION) == []


def test_egress_missing_keeps_bare_api_host(tmp_path: Path):
    """A bare (no-scheme) reference to a genuine api host is still caught."""
    ws = _make_workspace(tmp_path, {
        "scripts/c.py": "BASE = 'api.github.com'\n",
    })
    manifest = {
        "id": "i-app", "name": "Updater",
        "files": [{"path": "scripts/c.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/c.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    missing = _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION)
    assert len(missing) == 1
    assert missing[0].entry_value == "api.github.com"


def test_egress_missing_skips_xml_namespace_false_positive(tmp_path: Path):
    """An OOXML xmlns URI literal must NOT yield an egress finding."""
    ws = _make_workspace(tmp_path, {
        "scripts/docx.py": (
            'NS = \'<w:pBdr xmlns:w="http://schemas.openxmlformats.org'
            '/wordprocessingml/2006/main">\'\n'
        ),
    })
    manifest = {
        "id": "i-app", "name": "Document Generation",
        "files": [{"path": "scripts/docx.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/docx.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION) == []


def test_egress_missing_skips_loopback_gateway_false_positive(tmp_path: Path):
    """The in-pod gateway URL (loopback) must NOT yield an egress finding."""
    ws = _make_workspace(tmp_path, {
        "scripts/w.py": 'GATEWAY_URL = "http://127.0.0.1:18800/tools/invoke"\n',
    })
    manifest = {
        "id": "i-app", "name": "Watchdog System",
        "files": [{"path": "scripts/w.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/w.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION) == []


def test_egress_missing_still_fires_for_genuine_undeclared_host(tmp_path: Path):
    """Regression guard: a real undeclared host still produces a finding,
    so the gate doesn't suppress genuine security-hygiene findings."""
    ws = _make_workspace(tmp_path, {
        "scripts/s.py": "post('https://hooks.slack.com/services/T/B/X', data)",
    })
    manifest = {
        "id": "i-app", "name": "Slack Integration",
        "files": [{"path": "scripts/s.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/s.py"], "network_egress": []},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    missing = _by_kind(findings, KIND_EGRESS_MISSING_DECLARATION)
    assert len(missing) == 1
    assert missing[0].entry_value == "hooks.slack.com"


# ── KIND_EXEC_OVERKILL_WILDCARD ──────────────────────────────────────────────


def test_exec_overkill_wildcard_when_workspace_has_extras(tmp_path: Path):
    """Wildcard `scripts/*.py` matches 5 workspace files but only 1 is
    declared in manifest's files[]. Should flag as overkill."""
    ws = _make_workspace(tmp_path, {
        "scripts/declared.py": "# declared",
        "scripts/extra1.py": "# extra",
        "scripts/extra2.py": "# extra",
        "scripts/extra3.py": "# extra",
        "scripts/extra4.py": "# extra",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/declared.py", "layer": "script"}],
        "permissions": {"exec": ["scripts/*.py"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    overkill = _by_kind(findings, KIND_EXEC_OVERKILL_WILDCARD)
    assert len(overkill) == 1
    assert overkill[0].entry_value == "scripts/*.py"
    assert overkill[0].severity == "info"
    # The proposal should know how big the spillover is
    assert overkill[0].meta.get("workspace_match_count", 0) >= 5
    assert overkill[0].meta.get("declared_match_count", 0) == 1


def test_exec_overkill_skipped_when_match_set_close(tmp_path: Path):
    """If wildcard matches roughly what's declared, don't flag."""
    ws = _make_workspace(tmp_path, {
        "scripts/a.py": "# a",
        "scripts/b.py": "# b",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [
            {"path": "scripts/a.py", "layer": "script"},
            {"path": "scripts/b.py", "layer": "script"},
        ],
        "permissions": {"exec": ["scripts/*.py"]},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EXEC_OVERKILL_WILDCARD) == []


# ── KIND_EGRESS_OVERKILL_WILDCARD ────────────────────────────────────────────


def test_egress_overkill_wildcard_when_only_one_grep_match(tmp_path: Path):
    """*.example.com declared but only api.example.com grep-found."""
    ws = _make_workspace(tmp_path, {
        "scripts/run.py": "url = 'https://api.example.com/x'",
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/run.py"],
            "network_egress": ["*.example.com"],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    overkill = _by_kind(findings, KIND_EGRESS_OVERKILL_WILDCARD)
    assert len(overkill) == 1


def test_egress_overkill_skipped_when_multiple_subhosts(tmp_path: Path):
    """*.example.com grep-matches both api.* and console.* — don't flag."""
    ws = _make_workspace(tmp_path, {
        "scripts/run.py": (
            "a = 'https://api.example.com'\n"
            "b = 'https://console.example.com'\n"
        ),
    })
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/run.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/run.py"],
            "network_egress": ["*.example.com"],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert _by_kind(findings, KIND_EGRESS_OVERKILL_WILDCARD) == []


# ── No permissions block → no findings ───────────────────────────────────────


def test_no_permissions_block_returns_empty(tmp_path: Path):
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/foo.py", "layer": "script"}],
        # no permissions key
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert findings == []


def test_only_underscore_keys_returns_empty(tmp_path: Path):
    """A `permissions` block with only metadata (no declarations) is
    treated as missing — bootstrapper's territory."""
    ws = _make_workspace(tmp_path, {"scripts/foo.py": "# foo"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/foo.py", "layer": "script"}],
        "permissions": {"_note": "block deliberately empty"},
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    assert findings == []


# ── Affirmation propagation ─────────────────────────────────────────────────


def test_affirmation_key_format():
    """The Finding.affirmation_key() format is contractual — Phase C's
    auto-affirm mechanism (if/when built) needs to match against it."""
    f = Finding(
        kind=KIND_EXEC_UNUSED, bot_id="team_bot_a", app_id="i-app",
        app_name="App", entry_kind="exec", entry_value="scripts/foo.py",
        severity="warn", rationale="",
    )
    assert f.affirmation_key() == "permission_exec_unused:exec:scripts/foo.py"


def test_affirmation_skips_multiple_kinds(tmp_path: Path):
    """A single _affirmed entry skips exactly its kind+kind+value match,
    not other findings on the same entry."""
    ws = _make_workspace(tmp_path, {"scripts/real.py": "# real"})
    manifest = {
        "id": "i-app",
        "name": "App",
        "files": [{"path": "scripts/real.py", "layer": "script"}],
        "permissions": {
            "exec": ["scripts/ghost.py", "scripts/another-ghost.py"],
            "_affirmed": [
                "permission_exec_unused:exec:scripts/ghost.py",
            ],
        },
    }
    findings = find_per_app_candidates(manifest, ws, "team_bot_a")
    unused = _by_kind(findings, KIND_EXEC_UNUSED)
    patterns = {f.entry_value for f in unused}
    assert "scripts/ghost.py" not in patterns  # affirmed
    assert "scripts/another-ghost.py" in patterns  # NOT affirmed
