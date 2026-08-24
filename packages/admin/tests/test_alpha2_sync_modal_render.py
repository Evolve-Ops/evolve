"""ALPHA-2 — the sync modal stops green-checking a pod whose scan skipped a phase.

Audit B2a's most operator-visible half, and the one an independent review found
still unfixed after the first cut: `POST /api/applications/sync/pod` was carrying
the degradation honestly, and `runSyncPod`'s modal — the screen the operator is
actually looking at when they click *Sync all bots* — rendered

    ✓ Every bot is up to date — no new apps, no manifest drift.

in green, because its early return keyed on `totalDiscovered === 0` and a scan
with no model behind it finds zero by construction.

Executed, not grepped: `apps.js` runs in a Node VM whose unresolved globals are
auto-stubbed, so the REAL `runSyncPod` / `_renderSyncResult` build the HTML these
assertions read. `fetch` is the only thing deliberately faked — it is the seam
that supplies the payload under test.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PAGE = _REPO / "packages/admin/evolve_admin/web/static/js/pages/apps.js"

_ADMIN_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import scan_provenance as sp  # noqa: E402
from evolve_admin.applications import sync as sync_mod  # noqa: E402
from evolve_admin.applications.reflect import ReflectResult  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to execute the SPA renderer",
)

_HARNESS = textwrap.dedent("""
    const fs = require('fs'), vm = require('vm');

    const nodes = {};
    function node(id) {
      if (!nodes[id]) nodes[id] = {id, innerHTML: '', textContent: '', value: '',
        classList: {add(){}, remove(){}, contains(){ return false; }},
        setAttribute(){}, querySelector(){ return null; },
        querySelectorAll(){ return []; }, style: {}};
      return nodes[id];
    }

    const payload = JSON.parse(process.argv[3]);

    const real = {
      document: {
        getElementById: (id) => node(id), querySelector: () => null,
        querySelectorAll: () => [], addEventListener: () => {},
        createElement: () => node('tmp'), body: node('body'),
      },
      escHtml: (s) => String(s == null ? '' : s).replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
      botLabel: (b) => b,
      toast: (msg, kind) => { real.__toasts.push(kind + '|' + msg); },
      // The seam under test: hand the renderer the response, refuse every
      // follow-up call (the recon fetches) so nothing overwrites the result.
      fetch: async (url) => (String(url).indexOf('/sync') !== -1)
        ? {ok: true, json: async () => payload}
        : {ok: false, status: 500, json: async () => ({})},
      __toasts: [],
      console,
    };
    const sandbox = new Proxy(real, {
      has: () => true,
      get(t, k) {
        if (k in t) return t[k];
        if (k === Symbol.unscopables) return undefined;
        // `has` claims every identifier, which otherwise shadows the language
        // itself — Object, JSON, Promise. Hand those back before stubbing.
        if (typeof globalThis[k] !== 'undefined') return globalThis[k];
        // Anything else the page reaches for that this harness does not model:
        // a no-op rather than a ReferenceError, so one unmodelled sibling
        // global cannot mask the branch under test.
        return function stub() { return ''; };
      },
      set(t, k, v) { t[k] = v; return true; },
    });
    const ctx = vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);

    // Both renderers finish by refreshing the Apps grid. That is a real side
    // effect on an unrelated surface and it is not what these tests measure —
    // neutralise it AFTER the file has defined it, so everything the modal
    // itself renders still runs for real.
    real.loadCapabilities = () => {};
    real.appsLoadDiscovered = () => {};

    (async () => {
      const mode = process.argv[4];
      if (mode === 'pod') {
        await real.runSyncPod();
      } else {
        real._renderSyncResult('bot-a', payload);
      }
      // Let the renderer's own trailing awaits settle before reading.
      await new Promise(r => setTimeout(r, 50));
      process.stdout.write(JSON.stringify({
        html: node('reflect-modal-body').innerHTML,
        toasts: real.__toasts,
      }));
    })();
""")


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("alpha2-sync-js") / "render.js"
    path.write_text(_HARNESS)
    return path


def render(harness: Path, payload: dict, *, mode: str = "pod") -> dict:
    result = subprocess.run(
        ["node", str(harness), str(_PAGE), json.dumps(payload), mode],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ── Fixtures built by the REAL producers ────────────────────────────────────
#
# `sync._result` and `scan_provenance.explain_reason` shape these, so a rename
# on either side breaks this test rather than silently blanking the modal.


def _bot_result(bot_id: str, *, degraded: bool, discovered: int = 0,
                unverified: bool = False) -> dict:
    if unverified:
        return sync_mod._result(
            bot_id, "escalated",
            ("Found 9 uncovered code file(s)/dir(s) → ran a scan whose own "
             "record could not be read; discovered 0 app(s)"),
            0, [], ReflectResult(bot_id=bot_id),
            llm_phase=sync_mod.LLM_PHASE_UNKNOWN,
        )
    if degraded:
        note, remedy = sp.explain_reason(sp.REASON_NO_LLM_KEY)
        extra = {
            "llm_phase": sync_mod.LLM_PHASE_SKIPPED_DEGRADED, "llm_degraded": True,
            "llm_degraded_reason": sp.REASON_NO_LLM_KEY,
            "llm_degraded_note": note, "llm_degraded_remedy": remedy,
        }
        reason = ("Found 9 uncovered code file(s)/dir(s) → ran a structural scan "
                  "only (no working model, so the part that recognises apps was "
                  "skipped); discovered 0 app(s)")
    else:
        extra = {"llm_phase": sync_mod.LLM_PHASE_RAN}
        reason = f"Found 2 uncovered code file(s)/dir(s) → ran full scan; discovered {discovered} app(s)"
    return sync_mod._result(
        bot_id, "escalated", reason, discovered, [],
        ReflectResult(bot_id=bot_id), **extra,
    )


def _pod_payload(specs: list[tuple[str, bool]], unverified: tuple = ()) -> dict:
    """Mirrors what routes_applications_sync's pod route assembles."""
    bots = [_bot_result(b, degraded=d) for b, d in specs]
    bots += [_bot_result(b, degraded=False, unverified=True) for b in unverified]
    grouped: dict[str, dict] = {}
    for r in bots:
        if not r.get("llm_degraded"):
            continue
        slot = grouped.setdefault(r["llm_degraded_reason"], {
            "reason": r["llm_degraded_reason"], "note": r["llm_degraded_note"],
            "remedy": r["llm_degraded_remedy"], "bots": [],
        })
        slot["bots"].append(r["bot_id"])
    return {
        "ok": True, "total_discovered": sum(r["discovered_count"] for r in bots),
        "total_findings": 0, "escalated_bots": len(bots),
        "degraded_bots": sorted(r["bot_id"] for r in bots if r.get("llm_degraded")),
        "unverified_bots": sorted(
            r["bot_id"] for r in bots
            if r.get("llm_phase") == sync_mod.LLM_PHASE_UNKNOWN),
        "llm_degraded_reasons": [grouped[k] for k in sorted(grouped)],
        "aggregate_counts": {}, "bots": bots,
    }


# ── The pod modal ───────────────────────────────────────────────────────────


def test_a_degraded_pod_never_gets_the_green_up_to_date_check(harness):
    """The regression an independent review caught in the first cut."""
    out = render(harness, _pod_payload(
        [("personal-bot", True), ("team-bot-a", True), ("admin-bot", True)]))
    assert "Every bot is up to date" not in out["html"], (
        "B2a: zero apps under a scan that never ran its model phase is not "
        "'up to date' — it is a consequence of the skip"
    )
    assert "Evolve cannot vouch for this whole sync." in out["html"]
    assert "no working provider key" in out["html"]
    assert "Plugins → Credentials" in out["html"]


def test_a_degraded_pod_counts_structural_scans_separately_from_full_ones(harness):
    out = render(harness, _pod_payload(
        [("personal-bot", True), ("team-bot-a", False)]))
    assert "1 full scan" in out["html"]
    assert "1 structural-only scan" in out["html"]
    assert "2 full scans" not in out["html"], (
        "the old copy counted every escalation as a full scan"
    )


def test_a_healthy_pod_still_gets_its_green_check(harness):
    out = render(harness, _pod_payload(
        [("personal-bot", False), ("team-bot-a", False)]))
    assert "Every bot is up to date" in out["html"]
    assert "Evolve cannot vouch for this whole sync." not in out["html"]


def test_a_degraded_bot_row_is_not_a_green_tick(harness):
    out = render(harness, _pod_payload(
        [("personal-bot", True), ("team-bot-a", False)]))
    assert "structural scan only" in out["html"]
    assert "✓ structural scan only" not in out["html"]


def test_one_reason_shared_by_three_bots_is_one_line_in_the_modal(harness):
    out = render(harness, _pod_payload(
        [("a", True), ("b", True), ("c", True)]))
    assert out["html"].count("no working provider key") == 1
    assert "On 3 bots (a, b, c)" in out["html"]


def test_the_modal_never_prints_a_reason_key(harness):
    out = render(harness, _pod_payload([("a", True)]))
    for field_name in ("llm_degraded", "no_llm_provider_key", "llm_phase"):
        assert field_name not in out["html"], f"{field_name} is a field name on screen"


# ── llm_phase "unknown": the hole pass 1's fix left open ────────────────────
#
# The producer keeps ``unknown`` distinct from ``ran`` precisely so a scan whose
# own record could not be read is never reported as a good one. The first cut of
# this fix branched on ``llm_degraded``, which is False for ``unknown`` — so the
# green tick came back for exactly the case the producer went out of its way to
# preserve. Reachable: ``_write_status`` swallows OSError, and the manifests dir
# and its ACL grant are both best-effort, so a bot whose dir ``evolve`` cannot
# write is a scan that wrote nothing.


def test_an_unconfirmable_scan_never_gets_the_green_up_to_date_check(harness):
    out = render(harness, _pod_payload([], unverified=("bot-a", "bot-b")))
    assert "Every bot is up to date" not in out["html"], (
        "a scan Evolve could not confirm is not a scan Evolve can vouch for"
    )
    assert "could not confirm" in out["html"]
    assert "no readable record" in out["html"]


def test_an_unconfirmable_scan_is_not_counted_as_a_full_scan(harness):
    out = render(harness, _pod_payload([("bot-a", False)], unverified=("bot-b",)))
    assert "1 full scan" in out["html"]
    assert "1 scan Evolve could not confirm" in out["html"]
    assert "2 full scans" not in out["html"]


def test_the_full_scan_count_never_goes_negative(harness):
    """degraded + unverified could exceed the escalated count if a producer
    change ever let them overlap; the count must not render as -1."""
    payload = _pod_payload([("bot-a", True)], unverified=("bot-b",))
    payload["escalated_bots"] = 1
    out = render(harness, payload)
    assert "-1 full scan" not in out["html"]


def test_a_mixed_pod_names_both_kinds_of_uncertainty(harness):
    out = render(harness, _pod_payload([("bot-a", True)], unverified=("bot-b",)))
    assert "no working provider key" in out["html"]
    assert "no readable record" in out["html"]
    assert "Evolve cannot vouch for this whole sync." in out["html"]


def test_an_unconfirmable_single_bot_sync_does_not_say_scan_complete(harness):
    out = render(harness, _bot_result("bot-a", degraded=False, unverified=True),
                 mode="one")
    assert "scan complete" not in out["html"]
    assert "could not confirm what it did" in out["html"]
    assert out["toasts"][0].startswith("warn|"), out["toasts"]


def test_an_older_server_response_degrades_to_the_pre_chip_behaviour(harness):
    """No llm_* keys at all: no crash, no blank banner, no invented warning."""
    payload = _pod_payload([("bot-a", False), ("bot-b", False)])
    for b in payload["bots"]:
        for k in ("llm_phase", "llm_degraded", "llm_degraded_reason",
                  "llm_degraded_note", "llm_degraded_remedy"):
            b.pop(k, None)
    payload.pop("degraded_bots", None)
    payload.pop("unverified_bots", None)
    payload.pop("llm_degraded_reasons", None)
    out = render(harness, payload)
    assert "Every bot is up to date" in out["html"]
    assert "cannot vouch" not in out["html"]


# ── The per-bot modal ───────────────────────────────────────────────────────


def test_a_degraded_single_bot_sync_does_not_say_scan_complete(harness):
    out = render(harness, _bot_result("bot-a", degraded=True), mode="one")
    assert "scan complete" not in out["html"]
    assert "the part that finds apps did not run" in out["html"]
    assert "no working provider key" in out["html"]


def test_a_degraded_single_bot_sync_toasts_a_warning_not_a_tick(harness):
    out = render(harness, _bot_result("bot-a", degraded=True), mode="one")
    assert out["toasts"], "the renderer raises a toast on every path"
    assert out["toasts"][0].startswith("warn|"), out["toasts"]


def test_a_healthy_single_bot_sync_still_says_scan_complete(harness):
    out = render(harness, _bot_result("bot-a", degraded=False, discovered=2),
                 mode="one")
    assert "Found 2 new apps — scan complete" in out["html"]
    assert out["toasts"][0].startswith("ok|"), out["toasts"]
