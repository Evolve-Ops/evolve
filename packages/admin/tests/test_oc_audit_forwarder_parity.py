"""tests/test_oc_audit_forwarder_parity.py — the THREE OC-audit forwarders agree.

OpenClaw's native security-audit findings reach operators through three
forwarders, each historically carrying its own hand-copied "drop/demote this OC
advisory" logic:

  1. analyzer/audit.py::audit_oc_security          (daemon → Signal store)
  2. routes_oc.py::_audit_run_one                  (Security page)
  3. evo/handlers/oc_audit.py::_normalize_findings (evo "what can my bot do?" tray)

The duplicated copies were the "keep the two lists in sync" hazard that caused
the mask-FP drift bug (memory:
feedback_three_oc_audit_forwarders_must_share_suppression). The live forwarders
(2) and (3) now route through ONE helper —
``analyzer/audit.py::normalize_oc_finding`` — and the one rule shared with the
daemon (1), the member-bot "full" exec drop, lives in the policy table (1)
already consults.

This file pins that the single source and its callers agree on a representative
finding set:
  - the helper's drop/demote decisions on each representative finding;
  - the tray forwarder (real ``_normalize_findings``) reflects the helper's
    no-context decision finding-for-finding;
  - the documented per-surface difference: the two context-dependent demotions
    (proxy-header on a loopback gateway, model below-recommended) fire on the
    Security page (which passes config context) but NOT on the tray (which
    passes none);
  - the live-drop set is a subset of the daemon's policy-override table, so the
    security_full drop can't silently diverge across the three forwarders.
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
    """Perms seam where exactly ``_MASKED_PATH`` is a getfacl-proven evolve-read
    mask artifact; every other path is a real grant. Matches the fixture in
    test_oc_audit_mask_suppression.py."""
    from runtime.perms import FakePerms, set_perms

    class _MaskFake(FakePerms):
        def acl_masked_owner_only(self, path) -> bool:  # type: ignore[override]
            return str(path) == _MASKED_PATH

    set_perms(_MaskFake())
    try:
        yield
    finally:
        set_perms(None)


def _finding(check_id, detail="", severity="warn", title="x", remediation=""):
    return {
        "checkId": check_id,
        "title": title,
        "detail": detail,
        "severity": severity,
        "remediation": remediation,
    }


# A representative finding set exercising every rule in the shared helper.
def _representative_findings():
    return {
        # mask-FP on a mask-prone check id + getfacl-proven path → DROP everywhere
        "mask_fp": _finding(
            "fs.config.perms_group_readable",
            f"{_MASKED_PATH} mode=650; config can contain tokens",
        ),
        # genuine world-readable (real other::r, NOT a mask-prone id) → KEEP warning
        "world_readable": _finding(
            "fs.config.perms_world_readable",
            f"{_MASKED_PATH} mode=644; world-readable",
        ),
        # creds-dir CRITICAL (#3213 carves creds out of the ACL) → KEEP critical
        "creds_critical": _finding(
            "fs.credentials_dir.perms_readable",
            "/home/evo/.openclaw/credentials mode=755; readable by others",
            severity="critical",
            title="Credentials dir is readable by others",
        ),
        # member-bot "full" exec → DROP everywhere (policy override)
        "security_full": _finding(
            "tools.exec.security_full_configured",
            title='tools.exec.security="full"',
            remediation="Prefer allowlist with ask prompts.",
        ),
        # generic multi-user prose → DEMOTE to info everywhere (context-free)
        "multi_user": _finding(
            "security.trust_model.x",
            title="Multi-user access detected — review shared config",
        ),
        # proxy-header advisory → context-dependent demote (Security page only)
        "proxy": _finding(
            "gateway.proxy_headers",
            title="Reverse proxy detected without trusted proxies configured",
            remediation="Set gateway.trustedProxies / proxy headers.",
        ),
        # model below-recommended → context-dependent demote (Security page only)
        "below_rec": _finding(
            "models.weak_tier_x",
            title="Model tier is below recommended for this role",
        ),
        # genuine warning that matches no rule → KEEP warning everywhere
        "genuine": _finding(
            "auth.token.expiry",
            title="API token nearing expiry",
        ),
    }


# ── 1. The single-source helper's decisions ─────────────────────────────────


def test_helper_decisions_on_representative_set(mask_seam):
    import audit

    f = _representative_findings()

    # drops
    d = audit.normalize_oc_finding(f["mask_fp"])
    assert d.drop and d.reason == "acl_mask_fp"
    d = audit.normalize_oc_finding(f["security_full"])
    assert d.drop and d.reason == "policy_override"

    # keeps (no demote)
    assert audit.normalize_oc_finding(f["world_readable"]) == audit.OCFindingDecision(
        drop=False, severity="warning", raw_severity="warning", demoted=False,
    )
    assert audit.normalize_oc_finding(f["creds_critical"]).severity == "critical"
    assert not audit.normalize_oc_finding(f["creds_critical"]).drop
    assert audit.normalize_oc_finding(f["genuine"]).severity == "warning"

    # context-free demote
    d = audit.normalize_oc_finding(f["multi_user"])
    assert d.severity == "info" and d.demoted and not d.drop


def test_helper_context_dependent_demotions(mask_seam):
    import audit

    f = _representative_findings()

    # proxy-header: demoted ONLY when a loopback gateway_bind is supplied
    assert audit.normalize_oc_finding(f["proxy"]).severity == "warning"
    assert audit.normalize_oc_finding(
        f["proxy"], gateway_bind="127.0.0.1"
    ).severity == "info"
    # a non-loopback bind does NOT demote
    assert audit.normalize_oc_finding(
        f["proxy"], gateway_bind="0.0.0.0"
    ).severity == "warning"

    # below-recommended: demoted when routing is on OR primary is a good tier
    assert audit.normalize_oc_finding(f["below_rec"]).severity == "warning"
    assert audit.normalize_oc_finding(
        f["below_rec"], routing_enabled=True
    ).severity == "info"
    assert audit.normalize_oc_finding(
        f["below_rec"], primary_model="anthropic/claude-opus-4-8"
    ).severity == "info"
    # a haiku primary is not a recommended tier → stays a warning
    assert audit.normalize_oc_finding(
        f["below_rec"], primary_model="anthropic/claude-haiku-4-5"
    ).severity == "warning"


# ── 2. The tray forwarder reflects the helper (no context) ───────────────────


def test_tray_forwarder_matches_helper_no_context(mask_seam):
    """The real tray ``_normalize_findings`` must produce exactly what calling
    the shared helper with NO surface context produces — drop the drops, keep
    everything else at the helper's severity."""
    import audit
    from evolve_admin.evo.handlers.oc_audit import _normalize_findings

    f = _representative_findings()
    ordered = [
        f["mask_fp"], f["world_readable"], f["creds_critical"], f["security_full"],
        f["multi_user"], f["proxy"], f["below_rec"], f["genuine"],
    ]

    expected = []
    for raw in ordered:
        d = audit.normalize_oc_finding(raw)  # no context — same as the tray
        if d.drop:
            continue
        expected.append((raw["title"], d.severity))

    out = _normalize_findings(ordered)
    got = [(o["message"], o["severity"]) for o in out]

    assert got == expected
    # concretely: mask + security_full dropped; multi-user demoted; proxy and
    # below-recommended KEPT as warnings (no context on the tray).
    by_title = {o["message"]: o["severity"] for o in out}
    assert "tools.exec.security=\"full\"" not in by_title
    assert all("mode=650" not in o["message"] for o in out)
    assert by_title["Multi-user access detected — review shared config"] == "info"
    assert by_title["Reverse proxy detected without trusted proxies configured"] == "warning"
    assert by_title["Model tier is below recommended for this role"] == "warning"
    assert by_title["API token nearing expiry"] == "warning"
    assert by_title["Credentials dir is readable by others"] == "critical"


# ── 3. The Security-page surface (helper + context) vs the tray ──────────────


def test_security_page_context_vs_tray_difference(mask_seam):
    """Pin the ONLY intended divergence between the two live forwarders: the
    Security page passes config context, so the proxy-header and
    below-recommended advisories demote there; the tray passes none, so they
    stay warnings. Every context-free rule (mask drop, full drop, multi-user
    demote) agrees on both surfaces."""
    import audit

    f = _representative_findings()

    def security_page(raw):
        # The Security-page forwarder calls the helper with this surface's
        # config context (loopback gateway, routing on).
        return audit.normalize_oc_finding(
            raw, gateway_bind="127.0.0.1", routing_enabled=True,
            primary_model="anthropic/claude-opus-4-8",
        )

    def tray(raw):
        return audit.normalize_oc_finding(raw)

    # context-free rules: identical decisions on both surfaces
    for key in ("mask_fp", "security_full", "multi_user", "world_readable",
                "creds_critical", "genuine"):
        sp, tr = security_page(f[key]), tray(f[key])
        assert (sp.drop, sp.severity) == (tr.drop, tr.severity), key

    # context-dependent rules: demoted on the Security page, kept on the tray
    for key in ("proxy", "below_rec"):
        assert security_page(f[key]).severity == "info", key
        assert tray(f[key]).severity == "warning", key


# ── 4. The live-drop set can't diverge from the daemon's policy table ─────────


def test_live_drop_set_is_subset_of_daemon_policy_table():
    """The one drop rule shared with the daemon forwarder (the member-bot "full"
    exec advisory) must live in BOTH the live-drop set and the daemon's
    ``_OC_POLICY_OVERRIDES_SUPPRESS``, so all three forwarders drop it. A subset
    assertion makes the two impossible to silently diverge."""
    import audit

    assert audit._OC_LIVE_DROP_CHECK_IDS <= set(audit._OC_POLICY_OVERRIDES_SUPPRESS)
    # and the daemon's own predicate agrees on the shared id
    for check_id in audit._OC_LIVE_DROP_CHECK_IDS:
        assert audit._is_oc_policy_override(check_id) is True
