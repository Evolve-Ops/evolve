"""tests/test_control_registry.py — the D-CS7 meta-tests.

Guards the registry (tools/control-registry), its ratchet
(tools/control-coverage-ratchet + control-coverage-quarantine.txt), and the
one property the whole brief is about: a control's empty/unreachable source
must never read the same as a healthy one.

Placed under packages/admin/tests (not analyzer) because it exercises
evolve_admin.health directly; tools/control-registry and tools/ci_freshness
are pure-stdlib(+PyYAML) scripts importable from either package.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS_DIR = _REPO_ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _load_module(name: str, path: Path):
    # Explicit SourceFileLoader: control-registry has no .py suffix, so
    # spec_from_file_location can't infer a loader for it by extension.
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod  # dataclasses' _is_type looks the module up by name
    loader.exec_module(mod)
    return mod


control_registry = _load_module("control_registry", _TOOLS_DIR / "control-registry")


def test_every_control_has_two_fixtures():
    """Every ENFORCED (non-quarantined) control must have a known_good AND
    a known_bad fixture — the property a newly added control can never
    dodge (test_a_new_control_cannot_be_added_to_the_quarantine is the
    other half: it also can't dodge by hiding in the backlog list)."""
    controls = control_registry.discover_controls()
    quarantined = control_registry.load_quarantine()
    missing = [c.id for c in controls if c.id not in quarantined and not c.has_fixtures()]
    assert not missing, f"controls missing known_good/known_bad fixtures: {missing}"


def test_every_registered_control_has_both_fixtures():
    """Narrower sibling of the above: for controls that DO have fixtures,
    both files must actually parse as JSON objects, not just exist."""
    import json
    for c in control_registry.discover_controls():
        if not c.has_fixtures():
            continue
        for name in ("known_good.json", "known_bad.json"):
            data = json.loads((c.fixture_dir / name).read_text())
            assert isinstance(data, dict), f"{c.id}/{name} is not a JSON object"


def test_a_new_control_cannot_be_added_to_the_quarantine():
    """The quarantine is a closed, frozen backlog: every id in it must be a
    real discovered control that still lacks fixtures. A control cannot be
    added freshly (it isn't a discovered control at all), and one that has
    since grown fixtures must be deleted from the file, not left in."""
    controls = {c.id: c for c in control_registry.discover_controls()}
    quarantined = control_registry.load_quarantine()

    unknown = quarantined - controls.keys()
    assert not unknown, f"quarantine lists ids that are not discovered controls: {unknown}"

    stale = {cid for cid in quarantined if controls[cid].has_fixtures()}
    assert not stale, (
        "these quarantined controls now have fixtures and must be removed "
        f"from control-coverage-quarantine.txt, then re-freeze the baseline "
        f"(tools/control-coverage-ratchet --update-baseline): {stale}"
    )


def test_control_coverage_ratchet_refuses_growth(tmp_path: Path):
    """Copies the real ratchet into a throwaway repo shape (same pattern as
    test_file_size_ratchet.py) so REPO_ROOT resolves there — proves the
    no-growth contract without touching the real quarantine file."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    tool = tools_dir / "control-coverage-ratchet"
    tool.write_text((_TOOLS_DIR / "control-coverage-ratchet").read_text())
    tool.chmod(0o755)
    (tools_dir / "control-coverage-quarantine.txt").write_text("a\nb\nc\n")
    (tools_dir / "control-coverage-baseline.txt").write_text("3\n")

    def run():
        return subprocess.run(
            [sys.executable, str(tool)], cwd=tmp_path, capture_output=True, text=True,
        )

    at_baseline = run()
    assert at_baseline.returncode == 0

    (tools_dir / "control-coverage-quarantine.txt").write_text("a\nb\nc\nd\n")
    grown = run()
    assert grown.returncode == 1
    assert "grew" in grown.stdout

    (tools_dir / "control-coverage-quarantine.txt").write_text("a\nb\n")
    shrunk = run()
    assert shrunk.returncode == 0
    assert "BELOW baseline" in shrunk.stdout


def test_no_control_returns_ok_from_an_empty_source():
    """The specific bug behind all four D-CS7 instances. Drives both
    adopted controls' genuinely-empty-source shape directly (an absent log,
    an unreachable API) — neither may read as `ok`."""
    from evolve_admin import health
    v = health._repo_puller_verdict(Path("/nonexistent-for-test/repo-puller.log"))
    assert not v.is_ok
    assert v.is_unknown

    ci_freshness = _load_module("ci_freshness", _TOOLS_DIR / "ci_freshness.py")
    v2 = ci_freshness.evaluate_freshness(
        workflow="secret-history-scan.yml", job_name="Full-history secret scan",
        sched_reachable=False, disp_reachable=False, any_runs_found=False, latest_run=None,
        max_age_days=16,
    )
    assert not v2.is_ok
    assert v2.is_unknown
