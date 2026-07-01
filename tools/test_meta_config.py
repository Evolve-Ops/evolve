"""Mechanical tests for tools/meta_config.py (META:substrate Initiative 11/12, §16/§17).

Pure filesystem tests; no network, no live checkout — every case writes a fixture
`.claude/meta.json` (or omits it) into a tmp dir and resolves against it.

Run with:
  cd tools && python3 -m pytest test_meta_config.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# meta_config has a .py extension and lives beside this test, so a plain import works
# when pytest is run from tools/ (`cd tools && python3 -m pytest`). Guard the path so it
# also resolves if invoked from elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import meta_config  # noqa: E402


def _write_config(root: Path, payload) -> Path:
    claude = root / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    path = claude / "meta.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


# ── full / partial resolution ──────────────────────────────────────────────────


def test_present_config_full(tmp_path):
    """A complete config is returned verbatim across every field."""
    _write_config(tmp_path, {
        "repo_slug": "cjalden/calgraph",
        "registry_path": "docs/registry.md",
        "memory_slug": "*calgraph*",
        "deploy_model": "ship-on-merge",
        "preflight_cmd": "scripts/ci",
        "flaky_jobs_doc": "docs/flaky.md",
    })
    cfg = meta_config.load(tmp_path)
    assert cfg["repo_slug"] == "cjalden/calgraph"
    assert cfg["registry_path"] == "docs/registry.md"
    assert cfg["memory_slug"] == "*calgraph*"
    assert cfg["deploy_model"] == "ship-on-merge"
    assert cfg["preflight_cmd"] == "scripts/ci"
    assert cfg["flaky_jobs_doc"] == "docs/flaky.md"
    # accessors agree with load()
    assert meta_config.repo_slug(tmp_path) == "cjalden/calgraph"
    assert meta_config.registry_path(tmp_path) == "docs/registry.md"
    assert meta_config.memory_slug(tmp_path) == "*calgraph*"
    assert meta_config.deploy_model(tmp_path) == "ship-on-merge"
    assert meta_config.preflight_cmd(tmp_path) == "scripts/ci"
    assert meta_config.flaky_jobs_doc(tmp_path) == "docs/flaky.md"


def test_present_config_partial_fills_defaults(tmp_path):
    """Only repo_slug set → load-bearing fields fall back to Evolve's defaults, the OPTIONAL
    fields fall back to EMPTY (the DoD-degrade signal), deploy_model to pod-canary, and
    memory_slug DERIVES from the checkout dir basename."""
    root = tmp_path / "calgraph"
    _write_config(root, {"repo_slug": "cjalden/calgraph"})
    cfg = meta_config.load(root)
    assert cfg["repo_slug"] == "cjalden/calgraph"
    assert cfg["registry_path"] == meta_config.DEFAULT_REGISTRY_PATH
    assert cfg["deploy_model"] == meta_config.DEFAULT_DEPLOY_MODEL == "pod-canary"
    # OPTIONAL fields degrade to empty, NOT to Evolve's "tools/preflight".
    assert cfg["preflight_cmd"] == ""
    assert cfg["flaky_jobs_doc"] == ""
    # memory_slug derives from the checkout dir basename, NOT Evolve's *evolve*.
    assert cfg["memory_slug"] == "*calgraph*"


# ── absent / malformed → fall back (never raise) ────────────────────────────────


def test_absent_config_falls_back(tmp_path):
    """No .claude/meta.json anywhere → static defaults; memory_slug derives from the
    start dir basename (safe pre-migration behavior)."""
    root = tmp_path / "someproj"
    root.mkdir()
    cfg = meta_config.load(root)
    assert cfg["repo_slug"] == meta_config.DEFAULT_REPO_SLUG == "cjalden/evolve"
    assert cfg["registry_path"] == meta_config.DEFAULT_REGISTRY_PATH
    assert cfg["deploy_model"] == "pod-canary"
    assert cfg["preflight_cmd"] == ""
    assert cfg["flaky_jobs_doc"] == ""
    assert cfg["memory_slug"] == "*someproj*"


def test_malformed_json_falls_back(tmp_path):
    """Unparseable JSON → static defaults, never an exception; memory_slug derives from the
    config-root basename (the file exists, so it anchors the derive)."""
    root = tmp_path / "calgraph"
    _write_config(root, "{ not valid json ,,,")
    cfg = meta_config.load(root)
    assert cfg["repo_slug"] == "cjalden/evolve"
    assert cfg["deploy_model"] == "pod-canary"
    assert cfg["preflight_cmd"] == ""
    assert cfg["memory_slug"] == "*calgraph*"


def test_non_object_json_falls_back(tmp_path):
    """Valid JSON that isn't an object (e.g. a list) → static defaults."""
    root = tmp_path / "calgraph"
    _write_config(root, "[1, 2, 3]")
    cfg = meta_config.load(root)
    assert cfg["repo_slug"] == "cjalden/evolve"
    assert cfg["deploy_model"] == "pod-canary"
    assert cfg["memory_slug"] == "*calgraph*"


def test_blank_and_wrong_type_fields_fall_back(tmp_path):
    """Blank string / non-string field values are ignored in favor of the default."""
    root = tmp_path / "calgraph"
    _write_config(root, {
        "repo_slug": "", "registry_path": 123, "preflight_cmd": None,
        "deploy_model": "  ", "flaky_jobs_doc": [], "memory_slug": "",
    })
    cfg = meta_config.load(root)
    assert cfg["repo_slug"] == meta_config.DEFAULT_REPO_SLUG
    assert cfg["registry_path"] == meta_config.DEFAULT_REGISTRY_PATH
    assert cfg["preflight_cmd"] == ""
    assert cfg["flaky_jobs_doc"] == ""
    assert cfg["deploy_model"] == "pod-canary"
    assert cfg["memory_slug"] == "*calgraph*"  # blank explicit → derive


# ── deploy_model — the pod-only `live` bucket decoupling ─────────────────────────


def test_deploy_model_valid_values(tmp_path):
    for model in ("pod-canary", "ship-on-merge"):
        _write_config(tmp_path, {"deploy_model": model})
        assert meta_config.deploy_model(tmp_path) == model


def test_deploy_model_unknown_falls_back_to_pod_canary(tmp_path):
    """An unknown deploy_model is not silently honored — it falls back to the default so a
    typo can't put the tooling in an undefined bucket regime."""
    _write_config(tmp_path, {"deploy_model": "rolling-fleet"})
    assert meta_config.deploy_model(tmp_path) == "pod-canary"


def test_deploy_model_absent_is_pod_canary(tmp_path):
    _write_config(tmp_path, {"repo_slug": "cjalden/calgraph"})
    assert meta_config.deploy_model(tmp_path) == "pod-canary"


# ── memory_slug — the latent-bug fix (must not resolve to the WRONG ledger) ──────


def test_memory_slug_explicit_wins(tmp_path):
    """An explicit memory_slug is used verbatim, even if the checkout dir basename differs —
    the operator's declaration is authoritative."""
    root = tmp_path / "calgraph"
    _write_config(root, {"memory_slug": "*cg-ledger*"})
    assert meta_config.memory_slug(root) == "*cg-ledger*"


def test_memory_slug_derives_from_checkout_basename(tmp_path):
    """meta.json present but no memory_slug → derive `*<basename>*` from the checkout root,
    NOT Evolve's *evolve* (which would point a non-Evolve project at Evolve's ledger)."""
    root = tmp_path / "calgraph"
    _write_config(root, {"repo_slug": "cjalden/calgraph"})
    assert meta_config.memory_slug(root) == "*calgraph*"


def test_memory_slug_derives_from_cwd_when_no_config(tmp_path):
    """No meta.json at all → derive from the start (cwd) dir basename."""
    root = tmp_path / "widgets"
    root.mkdir()
    assert meta_config.memory_slug(root) == "*widgets*"


def test_memory_slug_derives_from_subdir_checkout_root(tmp_path):
    """Invoked from a subdir of the checkout: the derive anchors on the config ROOT (the dir
    holding .claude/meta.json), not the subdir, so it stays stable across the tree."""
    root = tmp_path / "calgraph"
    _write_config(root, {"repo_slug": "cjalden/calgraph"})
    deep = root / "packages" / "app"
    deep.mkdir(parents=True)
    assert meta_config.memory_slug(deep) == "*calgraph*"


def test_memory_slug_final_fallback_when_no_basename():
    """The degenerate empty-basename (filesystem root) case falls back to *evolve* — never
    an empty glob that would match nothing."""
    assert meta_config._derive_memory_slug(None, Path("/")) == meta_config.FALLBACK_MEMORY_SLUG
    assert meta_config.FALLBACK_MEMORY_SLUG == "*evolve*"


def test_memory_slug_never_empty_glob(tmp_path):
    """Whatever the inputs, memory_slug is a non-empty glob token — the whole point of the
    latent-bug fix (an empty/missing token silently resolved the ledger dir to nothing)."""
    for payload in ({"repo_slug": "x/y"}, "{bad json", "[1,2]", {"memory_slug": ""}):
        root = tmp_path / "proj"
        _write_config(root, payload)
        slug = meta_config.memory_slug(root)
        assert slug and slug.strip("*"), slug


# ── the resolution walk (worktree / subdir) ─────────────────────────────────────


def test_resolves_from_subdirectory(tmp_path):
    """A start dir below the config root still finds the checkout's config (worktree/subdir)."""
    _write_config(tmp_path, {"repo_slug": "cjalden/calgraph"})
    deep = tmp_path / "packages" / "app"
    deep.mkdir(parents=True)
    assert meta_config.repo_slug(deep) == "cjalden/calgraph"


# ── Evolve's own config must leave Evolve byte-for-byte unchanged ────────────────


def test_evolve_own_config_is_unchanged():
    """Evolve's own .claude/meta.json must restate every value explicitly so Evolve's
    behavior is unchanged by this parameterization — including the OPTIONAL fields whose
    default is now EMPTY (so an absent-field project degrades, but Evolve does not)."""
    repo_root = Path(__file__).resolve().parent.parent
    own = repo_root / ".claude" / "meta.json"
    assert own.is_file(), "Evolve must ship its own .claude/meta.json"
    data = json.loads(own.read_text())
    assert data["repo_slug"] == meta_config.DEFAULT_REPO_SLUG
    assert data["registry_path"] == meta_config.DEFAULT_REGISTRY_PATH
    assert data["memory_slug"] == "*evolve*"
    assert data["deploy_model"] == "pod-canary"
    assert data["preflight_cmd"] == "tools/preflight"
    assert data["flaky_jobs_doc"] == "docs/ci-flaky-jobs.md"


def test_evolve_resolves_to_evolve_values():
    """Resolving from the Evolve checkout root yields Evolve's values — the memory_slug is
    the explicit *evolve*, not a derive from the checkout dir (which in a worktree would be
    the worktree name)."""
    repo_root = Path(__file__).resolve().parent.parent
    cfg = meta_config.load(repo_root)
    assert cfg["repo_slug"] == "cjalden/evolve"
    assert cfg["memory_slug"] == "*evolve*"
    assert cfg["deploy_model"] == "pod-canary"
    assert cfg["preflight_cmd"] == "tools/preflight"


def test_evolve_declared_files_exist():
    """Evolve's declared registry / flaky-jobs docs actually ship (guards a typo in the
    declared paths that would silently degrade the DoD or orphan the registry)."""
    repo_root = Path(__file__).resolve().parent.parent
    assert meta_config.DEFAULT_REGISTRY_PATH == "docs/META-aspect-registry.md"
    assert (repo_root / meta_config.DEFAULT_REGISTRY_PATH).is_file(), \
        "Evolve must ship the extracted aspect-registry doc"
    assert (repo_root / "docs" / "ci-flaky-jobs.md").is_file(), \
        "Evolve's declared flaky_jobs_doc must exist"
