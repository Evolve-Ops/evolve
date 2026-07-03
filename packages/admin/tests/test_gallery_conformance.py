"""Gallery-wide conformance gate.

Deterministic sweep over EVERY entry in ``gallery/index.json`` (the builtin
gallery that ships with the repo). Per-app tests cover only a handful of
packages; this module is the fleet-wide net that catches a package landing
with an unparseable requirement, a dangling dependency, a stale index row,
or a script that doesn't compile — before an operator hits it live.

What it checks, and against what authority:

  1. Every package loads via the REAL loaders in
     ``evolve_admin.applications.gallery`` (no re-implemented parsing) and
     carries the required fields.
  2. index.json ↔ package-file ↔ tags-index.json consistency (paths exist,
     pkg_id/app_id/pkg_version/display_name/application_tags agree, no
     duplicates, no dangling tag references).
  3. requirements[] entries are parseable by the ACTUAL preflight checkers —
     exercised by running ``preflight_check`` for real against a nonexistent
     bot (every requirement must produce a definite state, never "unknown").
     Alternatives get a structural mirror check because ``_check_integration``
     drops non-satisfied alternative states on the floor.
  4. scheduled_actions[].mechanism values are ones
     ``forge_engine._materialize_scheduled_actions`` actually dispatches on,
     and mechanism-specific install blocks carry the fields the materializer
     hard-fails without.
  5. Bundled files-pack scripts compile (``py_compile`` / ``bash -n``).
  6. app_dependencies close over the gallery (no dangling deps).
  7. pkg_version parses with the real ``ids.parse_pkg_version``.

If a CURRENT package fails a check here, that is a finding about the
package, not about this test — fix the package (or mark the one check
xfail naming the package) rather than weakening the check globally.
"""

from __future__ import annotations

import inspect
import json
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REPO_ROOT = _ADMIN_DIR.parent.parent
_GALLERY_DIR = _REPO_ROOT / "gallery"

from evolve_admin.applications import gallery as gallery_mod  # noqa: E402
from evolve_admin.applications.gallery import (  # noqa: E402
    LEGACY_KEY_MIGRATION,
    _PKG_ID_RE,
    load_gallery_package,
    load_tags_index,
    preflight_check,
)
from evolve_admin.applications.ids import parse_pkg_version  # noqa: E402

# A bot id that exists on no host — preflight's on-disk probes
# (/Users/<bot>/.openclaw/...) all come back empty for it.
_FAKE_BOT = "evolve-conformance-nonexistent-bot"

# Fields every index.json row must carry (the admin UI and the install
# dispatcher read these straight off the index without opening the package).
_INDEX_REQUIRED_FIELDS = ("pkg_id", "pkg_version", "app_id", "display_name", "path")

# Fields every builtin package file must carry. Deliberately NOT
# gallery._REQUIRED_PKG_FIELDS: that set is the *import* gate for
# operator-uploaded packages and includes "objective", which 10 of the
# current builtin packages predate (see PR body — observation, not a
# runtime defect: the builtin load path never goes through import_package).
_PKG_REQUIRED_FIELDS = ("pkg_id", "pkg_version", "id", "name", "display_name", "build_spec")

# Mechanisms forge_engine._materialize_scheduled_actions dispatches on
# (packages/admin/evolve_admin/applications/forge_engine.py — the
# if/elif ladder in Phase 4.5). "external" is skipped-but-recognized;
# the oc_*_hook pair is recognized but DEPRECATED (materializer records
# a hard failure), so shipped packages must not use them either.
_KNOWN_MECHANISMS = frozenset({
    "external",
    "oc_heartbeat_instruction",
    "oc_session_instruction",
    "launchd",
    "launchd_python_signal",
    "crontab",
})
_DEPRECATED_MECHANISMS = frozenset({"oc_heartbeat_hook", "oc_session_hook"})


def _load_index() -> list[dict]:
    return json.loads((_GALLERY_DIR / "index.json").read_text(encoding="utf-8"))


_INDEX = _load_index()
_ENTRY_IDS = [e.get("pkg_id") or f"entry-{i}" for i, e in enumerate(_INDEX)]

_entry_param = pytest.mark.parametrize("entry", _INDEX, ids=_ENTRY_IDS)


def _load_pkg(entry: dict) -> dict:
    """Load the package file for an index entry via the real builtin loader."""
    pkg = gallery_mod._load_builtin_pkg(entry["pkg_id"])
    assert pkg is not None, (
        f"{entry['pkg_id']} ({entry.get('display_name')}): _load_builtin_pkg "
        f"returned None — package file missing or invalid JSON at "
        f"gallery/{entry.get('path')}"
    )
    return pkg


# ── 1+2: index rows, package files, cross-file consistency ──────────────────

def test_index_loads_via_real_loader():
    entries = gallery_mod._load_builtin_index()
    assert entries, (
        "_load_builtin_index() returned [] — gallery/index.json is missing "
        "or not valid JSON (the loader swallows the exception; the admin UI "
        "would show an empty gallery)"
    )
    assert entries == _INDEX


def test_no_duplicate_pkg_or_app_ids():
    seen_pkg: dict[str, str] = {}
    seen_app: dict[str, str] = {}
    for e in _INDEX:
        pkg_id, app_id = e.get("pkg_id", ""), e.get("app_id", "")
        assert pkg_id not in seen_pkg, (
            f"duplicate pkg_id {pkg_id!r} in index.json: "
            f"{seen_pkg[pkg_id]!r} and {e.get('path')!r}"
        )
        assert app_id not in seen_app, (
            f"duplicate app_id {app_id!r} in index.json: "
            f"{seen_app[app_id]!r} and {e.get('path')!r}"
        )
        seen_pkg[pkg_id] = e.get("path", "")
        seen_app[app_id] = e.get("path", "")


@_entry_param
def test_index_row_required_fields(entry):
    for field in _INDEX_REQUIRED_FIELDS:
        value = entry.get(field)
        assert isinstance(value, str) and value.strip(), (
            f"index.json row for {entry.get('pkg_id') or entry!r} is missing "
            f"or has an empty {field!r}"
        )
    assert _PKG_ID_RE.match(entry["pkg_id"]), (
        f"index.json pkg_id {entry['pkg_id']!r} does not match the canonical "
        f"p-<8 hex> shape ({_PKG_ID_RE.pattern})"
    )


@_entry_param
def test_index_path_exists_and_stem_matches(entry):
    pkg_path = _GALLERY_DIR / entry["path"]
    assert pkg_path.is_file(), (
        f"{entry['pkg_id']}: index path gallery/{entry['path']} does not exist"
    )
    assert pkg_path.stem == entry["pkg_id"], (
        f"{entry['pkg_id']}: package filename {pkg_path.name!r} stem must "
        f"equal the pkg_id (the builtin loaders locate packages by "
        f"<pkg_id>.json filename)"
    )


@_entry_param
def test_package_file_required_fields(entry):
    pkg = _load_pkg(entry)
    for field in _PKG_REQUIRED_FIELDS:
        assert pkg.get(field), (
            f"{entry['pkg_id']} (gallery/{entry['path']}): package file is "
            f"missing or has an empty {field!r}"
        )


@_entry_param
def test_index_row_matches_package_file(entry, tmp_path):
    pkg = _load_pkg(entry)
    # load_gallery_package is the loader every runtime consumer goes
    # through (update detection, install dispatch, preflight) — it must
    # agree with the direct builtin read.
    via_public = load_gallery_package(entry["pkg_id"], tmp_path)
    assert via_public == pkg, (
        f"{entry['pkg_id']}: load_gallery_package() and _load_builtin_pkg() "
        f"disagree — builtin/imported shadowing or legacy-migration bug"
    )
    for index_field, pkg_field in [
        ("pkg_id", "pkg_id"),
        ("app_id", "id"),
        ("pkg_version", "pkg_version"),
        ("display_name", "display_name"),
    ]:
        assert entry.get(index_field) == pkg.get(pkg_field), (
            f"{entry['pkg_id']} (gallery/{entry['path']}): index.json "
            f"{index_field!r} = {entry.get(index_field)!r} but the package "
            f"file's {pkg_field!r} = {pkg.get(pkg_field)!r} — the index is a "
            f"denormalized cache and has gone stale; update the index row"
        )
    assert list(entry.get("application_tags") or []) == list(pkg.get("application_tags") or []), (
        f"{entry['pkg_id']}: index application_tags differ from the package "
        f"file's — re-run scripts/backfill_application_tags.py --rebuild-index"
    )


def test_tags_index_consistent_both_ways():
    tags_index = load_tags_index()
    assert tags_index, (
        "load_tags_index() returned {} — gallery/tags-index.json missing or "
        "unreadable (tag filtering in the admin UI silently disappears)"
    )
    index_ids = {e["pkg_id"] for e in _INDEX}
    for tag, pkg_ids in tags_index.items():
        for pkg_id in pkg_ids:
            assert pkg_id in index_ids, (
                f"tags-index.json tag {tag!r} references pkg_id {pkg_id!r} "
                f"which is not in gallery/index.json"
            )
    # Reverse: every tag on every index row appears in the reverse index.
    for e in _INDEX:
        for tag in e.get("application_tags") or []:
            assert e["pkg_id"] in tags_index.get(tag, []), (
                f"{e['pkg_id']}: tag {tag!r} is on the index row but absent "
                f"from tags-index.json — re-run "
                f"scripts/backfill_application_tags.py --rebuild-index"
            )


def test_legacy_migration_targets_resolve():
    for retired, surviving in LEGACY_KEY_MIGRATION.items():
        assert gallery_mod._load_builtin_pkg(surviving) is not None, (
            f"LEGACY_KEY_MIGRATION maps retired {retired!r} to {surviving!r}, "
            f"which no longer resolves in the builtin gallery — old installs "
            f"of {retired!r} are stranded"
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "FINDING (2026-07-02, this gate's introduction): "
        "packages/gallery/catalog.json is fully disjoint from "
        "gallery/index.json — all 14 catalog pkg_ids (p-a1b2c3d4, ...) are "
        "placeholders that load_gallery_package() cannot resolve, so every "
        "`evo install <N>` picked from `evo gallery` dead-ends in "
        "'Package not found'. Needs a product decision (regenerate the evo "
        "catalog from the real gallery, or retire it); see the PR that "
        "added this test. strict=True so fixing the catalog forces removal "
        "of this marker."
    ),
)
def test_evo_catalog_pkg_ids_resolve_in_gallery():
    # Direct read: evo's _load_catalog() falls back to the deploy-checkout
    # path when the repo-relative file is missing, which is nondeterministic
    # in CI; the referential-integrity contract is about this file's content.
    catalog_path = _REPO_ROOT / "packages" / "gallery" / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert isinstance(catalog, list) and catalog
    index_ids = {e["pkg_id"] for e in _INDEX}
    dangling = [c.get("pkg_id") for c in catalog if c.get("pkg_id") not in index_ids]
    assert not dangling, (
        f"packages/gallery/catalog.json references pkg_ids absent from "
        f"gallery/index.json (evo install cannot resolve them): {dangling}"
    )


# ── 3: requirements[] parseability via the REAL preflight checkers ──────────

class _StubCompleted:
    returncode = 1
    stdout = ""
    stderr = ""


@pytest.fixture()
def _hermetic_preflight(monkeypatch, tmp_path):
    """Make preflight_check deterministic: no sudo, no host network.json.

    preflight's closures call ``subprocess.run`` (sudo /bin/cat fallback,
    system-tool probes) and ``config.load_network()`` (path-C google
    checks). Stub both so every probe comes back "not present" and the
    sweep asserts on PARSEABILITY (definite state), never on host state.
    """
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _StubCompleted())
    from evolve_admin import config as _cfg
    monkeypatch.setattr(_cfg, "load_network", lambda *a, **k: {"bots": {}})
    return tmp_path


@_entry_param
def test_preflight_parses_every_requirement(entry, _hermetic_preflight):
    result = preflight_check(entry["pkg_id"], _FAKE_BOT, _hermetic_preflight)
    assert "error" not in result, (
        f"{entry['pkg_id']}: preflight_check could not load the package: "
        f"{result.get('error')}"
    )
    for row in result["requirements"]:
        # The pm-inbox live trap: an unparseable requirement (e.g. ASCII
        # '->' instead of '→' in check_path/storage) yields state="unknown"
        # and an empty-id row, pegging ready_to_run=False forever with a
        # blank line in the operator UI.
        assert row.get("id"), (
            f"{entry['pkg_id']}: requirement row of type {row.get('type')!r} "
            f"has an empty id — the manifest entry is missing its id/key/"
            f"name field (renders as a blank row in the preflight UI): {row}"
        )
        assert row.get("state") != "unknown", (
            f"{entry['pkg_id']}: requirement {row.get('id')!r} "
            f"(type {row.get('type')!r}) came back state='unknown' from the "
            f"real preflight checkers — the declared shape is not parseable "
            f"by _check_integration/_check_secret/_check_system and would "
            f"peg ready_to_run=False forever. Checker message: "
            f"{row.get('message')!r}"
        )
        assert row.get("type") not in (
            "scheduled_actions", "apps_inherit_bot_llm", "privacy_scoping",
        ), (
            f"{entry['pkg_id']}: shipped package fails the "
            f"{row.get('type')} validator that preflight runs on every "
            f"install: {row.get('message')!r}"
        )


def _integration_check_parseable(req: dict) -> str | None:
    """Structural mirror of gallery._check_one_integration's dispatch.

    Returns None when the requirement would parse, else a reason string.
    Kept in lockstep with the checker: first-class ids (github, telegram),
    then 'openclaw.json → <dot.path>' (REAL '→', never ASCII '->'), then
    'network.json → bots[bot].google_integration...'.
    """
    # Match the real checker exactly: gallery._check_one_integration reads
    # `req.get("id", "")` WITHOUT stripping, so an id of " github " is NOT
    # treated as first-class github (it falls through to check_path and, with
    # none, returns "unknown" — silently dropped by _check_integration for an
    # alternative). Do not .strip() here, or the mirror would bless a dead id.
    integration_id = req.get("id") or ""
    if integration_id in ("github", "telegram"):
        return None
    check_path = (req.get("check_path") or "").strip()
    if "→" not in check_path:
        return (
            f"check_path {check_path!r} lacks the '→' separator "
            f"(_check_one_integration requires the real '→' character; "
            f"ASCII '->' is not recognized)"
        )
    head, _, tail = check_path.partition("→")
    file_part = head.strip()
    path_part = tail.strip().split("+", 1)[0].strip()
    if file_part == "openclaw.json" and path_part:
        return None
    if file_part == "network.json" and path_part.startswith("bots[bot].google_integration"):
        return None
    return (
        f"check_path {check_path!r} has unrecognized file root "
        f"{file_part!r} (supported: 'openclaw.json → <dot.path>', "
        f"'network.json → bots[bot].google_integration...')"
    )


@_entry_param
def test_integration_alternatives_parseable(entry):
    # _check_integration silently discards a non-satisfied alternative's
    # state, so an unparseable alternative never surfaces in preflight
    # output — it just makes the declared fallback auth path dead. Assert
    # the structural shape directly (primaries are covered both here and
    # by the live preflight sweep above).
    pkg = _load_pkg(entry)
    reqs = pkg.get("requirements") or {}
    integrations = reqs.get("integrations", []) if isinstance(reqs, dict) else []
    for req in integrations:
        problem = _integration_check_parseable(req)
        assert problem is None, (
            f"{entry['pkg_id']}: integration requirement "
            f"{req.get('id')!r}: {problem}"
        )
        for alt in req.get("alternatives") or []:
            problem = _integration_check_parseable(alt)
            assert problem is None, (
                f"{entry['pkg_id']}: alternative {alt.get('id')!r} of "
                f"integration {req.get('id')!r}: {problem}"
            )


@_entry_param
def test_secret_storage_paths_parseable(entry):
    pkg = _load_pkg(entry)
    reqs = pkg.get("requirements") or {}
    secrets = reqs.get("secrets", []) if isinstance(reqs, dict) else []
    for req in secrets:
        storage = (req.get("storage") or "").strip()
        assert "→" in storage, (
            f"{entry['pkg_id']}: secret {req.get('key')!r} storage "
            f"{storage!r} lacks the '→' separator — _check_secret returns "
            f"'unknown' for it (must be 'openclaw.json → <dot.path>' with "
            f"the real '→' character, not ASCII '->')"
        )


# ── 4: scheduled_actions mechanisms the forge materializer dispatches on ────

def test_known_mechanism_set_matches_forge_source():
    # Tripwire for silent drift: every mechanism string this module treats
    # as known must still appear in the materializer's dispatch ladder.
    from evolve_admin.applications import forge_engine
    src = inspect.getsource(forge_engine._materialize_scheduled_actions)
    for mech in sorted(_KNOWN_MECHANISMS | _DEPRECATED_MECHANISMS):
        assert f'"{mech}"' in src, (
            f"mechanism {mech!r} no longer appears in "
            f"_materialize_scheduled_actions — update _KNOWN_MECHANISMS/"
            f"_DEPRECATED_MECHANISMS in this test to match the dispatcher"
        )


@_entry_param
def test_scheduled_actions_mechanisms_and_install_shapes(entry):
    pkg = _load_pkg(entry)
    for action in pkg.get("scheduled_actions") or []:
        action_id = action.get("id") or "<no id>"
        mech = (action.get("mechanism") or "").strip()
        label = f"{entry['pkg_id']} scheduled_action {action_id!r}"
        assert mech not in _DEPRECATED_MECHANISMS, (
            f"{label}: mechanism {mech!r} is deprecated in v17 — the forge "
            f"materializer records a hard failure for it; migrate to "
            f"oc_*_instruction"
        )
        assert mech in _KNOWN_MECHANISMS, (
            f"{label}: mechanism {mech!r} is not one the forge materializer "
            f"dispatches on ({sorted(_KNOWN_MECHANISMS)}) — the install "
            f"would end 'unrecognized mechanism'"
        )
        install = action.get("install") or {}
        if mech == "launchd":
            # Mirror of the materializer's hard-fail gates: plist_label,
            # then command+non-empty schedule (canonical) or plist_xml
            # (legacy). Note mechanism:"launchd" itself is allowed here —
            # Linux materialization is a separate in-flight chip (#3392).
            assert (install.get("plist_label") or "").strip(), (
                f"{label}: launchd requires install.plist_label"
            )
            command = (install.get("command") or "").strip()
            schedule = install.get("schedule") or {}
            if command:
                assert isinstance(schedule, dict) and schedule, (
                    f"{label}: launchd with install.command requires a "
                    f"non-empty install.schedule dict"
                )
            else:
                assert install.get("plist_xml"), (
                    f"{label}: launchd requires install.command + "
                    f"install.schedule (canonical) or install.plist_xml "
                    f"(legacy)"
                )
        elif mech in ("oc_heartbeat_instruction", "oc_session_instruction"):
            # file/section_anchor/body must be non-empty STRINGS. The preflight
            # sweep can't catch a type-malformed value here: the wiring
            # validator does `(install.get("file") or "").strip()`, which raises
            # AttributeError on a non-empty dict/list, and preflight_check
            # swallows that exception (sa_result=None → no row), so the sweep
            # passes while the materializer hard-fails at install time.
            for field in ("file", "section_anchor", "body"):
                value = install.get(field)
                assert isinstance(value, str) and value.strip(), (
                    f"{label}: {mech} requires install.{field} to be a "
                    f"non-empty string (got {type(value).__name__}); the forge "
                    f"materializer fails the install otherwise"
                )
        elif mech == "launchd_python_signal":
            for field in ("label", "command", "signal_patterns"):
                assert install.get(field), (
                    f"{label}: launchd_python_signal requires install.{field}"
                )
        elif mech == "crontab":
            for field in ("schedule", "command"):
                assert (install.get(field) or "").strip(), (
                    f"{label}: crontab requires install.{field}"
                )


# ── 5: files-pack scripts compile ────────────────────────────────────────────

def _collect_pack_scripts() -> list[Path]:
    scripts: list[Path] = []
    for entry in _INDEX:
        files_dir = (_GALLERY_DIR / entry["path"]).parent / "files"
        if files_dir.is_dir():
            scripts.extend(
                p for p in sorted(files_dir.rglob("*"))
                if p.suffix in (".py", ".sh") and p.is_file()
            )
    return scripts


_PACK_SCRIPTS = _collect_pack_scripts()


@pytest.mark.parametrize(
    "script",
    _PACK_SCRIPTS,
    ids=[str(s.relative_to(_GALLERY_DIR)) for s in _PACK_SCRIPTS],
)
def test_files_pack_script_compiles(script, tmp_path):
    if script.suffix == ".py":
        try:
            py_compile.compile(str(script), cfile=str(tmp_path / "out.pyc"), doraise=True)
        except py_compile.PyCompileError as exc:
            pytest.fail(
                f"files-pack script gallery/{script.relative_to(_GALLERY_DIR)} "
                f"does not compile: {exc}"
            )
    else:
        bash = shutil.which("bash")
        assert bash, "bash not available on this runner"
        r = subprocess.run(
            [bash, "-n", str(script)], capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, (
            f"files-pack script gallery/{script.relative_to(_GALLERY_DIR)} "
            f"fails bash -n: {r.stderr.strip()}"
        )


# ── 6: dependency closure ────────────────────────────────────────────────────

@_entry_param
def test_app_dependencies_resolve(entry, tmp_path):
    pkg = _load_pkg(entry)
    for dep in pkg.get("app_dependencies") or []:
        dep_id = dep.get("pkg_id", "")
        assert dep_id, (
            f"{entry['pkg_id']}: app_dependencies entry "
            f"{dep.get('display_name')!r} has no pkg_id"
        )
        # The real resolver (handles LEGACY_KEY_MIGRATION redirects too).
        assert load_gallery_package(dep_id, tmp_path) is not None, (
            f"{entry['pkg_id']} ({entry.get('display_name')}): dependency "
            f"{dep_id!r} ({dep.get('display_name')!r}) does not resolve to "
            f"any builtin gallery package — dangling app_dependencies; "
            f"preflight would block the install forever"
        )


# ── 7: version format ────────────────────────────────────────────────────────

@_entry_param
def test_pkg_version_parses(entry):
    pkg = _load_pkg(entry)
    for where, version in [
        ("index.json row", entry.get("pkg_version")),
        (f"gallery/{entry['path']}", pkg.get("pkg_version")),
    ]:
        assert parse_pkg_version(version or "") is not None, (
            f"{entry['pkg_id']}: pkg_version {version!r} in {where} does not "
            f"match the calver shape YYYY.MM.DD-major.minor "
            f"(ids._PKG_VERSION_PATTERN) — compare_pkg_versions sorts it "
            f"older than everything, breaking update detection"
        )
