"""tests/test_oc_audit_mask_suppression.py — Linux ACL-mask FP suppression on the
two *live* OC-audit forwarders (Security page + evo tray).

Background (internal/diag-readable-findings-linux-2026-06-24.md): on the Linux
``evo`` pod the Security page showed advisories like "State dir is readable by
others" / "Config file is group-readable" for ``.openclaw`` files that are
genuinely owner-only. They are an artifact of Evolve's own evolve-read ACL —
adding ``user:evolve:r-x`` forces the POSIX mask, and ``st_mode``'s group triad
then DISPLAYS the mask, so OC's st_mode-based audit misreads them. The daemon
forwarder (analyzer/audit.py) already drops these via
``_is_acl_masked_perms_false_positive``; the two live forwarders did not, so the
operator-facing surfaces kept lying.

This pins:
  - the analyzer helper is importable through the same path the live
    forwarders use (``_import_analyzer("audit")``) — guards the wiring in
    ``routes_oc._audit_run_one`` (a Flask closure, exercised here at the helper
    seam) and the tray handler;
  - the tray forwarder ``_normalize_findings`` drops a getfacl-proven mask
    artifact while KEEPING a genuine exposure — a real ``other::r``
    (world-readable) and the credentials-dir CRITICAL #3213 carves out of the
    ACL must still fire.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADMIN_DIR = Path(__file__).parent.parent
_ANALYZER_DIR = _ADMIN_DIR.parent / "analyzer"
for _p in (str(_ADMIN_DIR), str(_ANALYZER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


_MASKED_PATH = "/home/evo/.openclaw/openclaw.json"


@pytest.fixture
def mask_seam():
    """Override the Perms seam so exactly ``_MASKED_PATH`` reads as a getfacl-
    proven evolve-read mask artifact; everything else is a real grant."""
    from runtime.perms import FakePerms, set_perms

    class _MaskFake(FakePerms):
        def acl_masked_owner_only(self, path) -> bool:  # type: ignore[override]
            return str(path) == _MASKED_PATH

    set_perms(_MaskFake())
    try:
        yield
    finally:
        set_perms(None)


def _finding(check_id: str, detail: str, severity: str = "warn", title: str = "x"):
    return {
        "checkId": check_id,
        "title": title,
        "detail": detail,
        "severity": severity,
        "remediation": "",
    }


def test_helper_resolves_through_live_import_path():
    """The live forwarders reach the analyzer helper via _import_analyzer; if
    that symbol moves/renames, both forwarders silently fail-open. Pin it."""
    from evolve_admin.web.server import _import_analyzer

    fn = _import_analyzer("audit")._is_acl_masked_perms_false_positive
    assert callable(fn)


def test_tray_forwarder_drops_mask_fp_keeps_real_exposure(mask_seam):
    from evolve_admin.evo.handlers.oc_audit import _normalize_findings

    findings = [
        # mask artifact on openclaw.json — DROP
        _finding("fs.config.perms_group_readable",
                 f"{_MASKED_PATH} mode=650; config can contain tokens"),
        # genuine world-readable (real other::r — NOT a mask-prone check) — KEEP
        _finding("fs.config.perms_world_readable",
                 f"{_MASKED_PATH} mode=644; world-readable", severity="warn"),
        # credentials-dir CRITICAL (#3213 carves creds out of the ACL, so any
        # read bit is real; check id not in the mask-prone set) — KEEP
        _finding("fs.credentials_dir.perms_readable",
                 "/home/evo/.openclaw/credentials mode=755; readable by others",
                 severity="critical",
                 title="Credentials dir is readable by others"),
    ]

    out = _normalize_findings(findings)
    kept_ids = {f["message"] for f in out}

    # the masked group-readable advisory is gone
    assert len(out) == 2
    # the genuine exposures survived
    assert any("readable by others" in m for m in kept_ids)  # creds critical
    assert any(f["severity"] == "critical" for f in out)


def test_tray_forwarder_keeps_mask_prone_id_when_seam_says_not_masked():
    """Same mask-prone check id, but the getfacl seam returns False (real
    group::r-x, or a non-.openclaw path): must NOT be suppressed."""
    from runtime.perms import set_perms

    set_perms(None)  # default fake: acl_masked_owner_only() is always False
    try:
        from evolve_admin.evo.handlers.oc_audit import _normalize_findings

        out = _normalize_findings([
            _finding("fs.config.perms_group_readable",
                     f"{_MASKED_PATH} mode=640; real group read"),
        ])
        assert len(out) == 1
    finally:
        set_perms(None)
