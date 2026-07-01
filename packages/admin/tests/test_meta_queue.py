"""Unit tests for tools/meta-queue.

`tools/meta-queue` is the deterministic projection of the cross-aspect META
decision queue (the read that `/queue`, `/reconcile`, `/coherence`, `/launch`
delegate to a throwaway subagent so it never lands in the operator's main
context). These tests pin the build rules — the auto-merge / held-merge
classification, gate↔decision dedup, operator-gate filtering, and snooze
handling — against a crafted fixture ledger dir so a rule can't silently drift.

The rules under test are the ones in `.claude/skills/queue/SKILL.md` and the
"decision queue (computed projection)" section of `docs/meta-ledger-schema.md`.

The tool is an extensionless script under tools/, so we load it by path.
"""

from __future__ import annotations

import datetime
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[3] / "tools" / "meta-queue"


def _load_tool():
    loader = importlib.machinery.SourceFileLoader("meta_queue", str(_TOOL))
    spec = importlib.util.spec_from_loader("meta_queue", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meta_queue"] = mod
    loader.exec_module(mod)
    return mod


mq = _load_tool()

TODAY = datetime.date(2026, 6, 18)

# ── fixture ledgers ────────────────────────────────────────────────────────
# Two aspects, filenames sorted "a-core" < "b-extra" so output ordering is
# deterministic. Between them they exercise every collect / skip / dedup rule.

LEDGER_CORE = {
    "aspect": "core",
    "updated": "2026-06-10",
    "chips": [
        # (A) Rescue — one per rescue reason, in precedence order.
        {"id": "S1", "title": "stalled one", "bucket": "stalled",
         "two_pass": "pending", "reversible": True, "pr": 111},
        {"id": "F1", "title": "fail one", "bucket": "open_green",
         "two_pass": "FAIL", "reversible": True, "pr": 112},
        {"id": "C1", "title": "concerns one", "bucket": "open_green",
         "two_pass": "CONCERNS", "reversible": True, "pr": 113},
        {"id": "IR1", "title": "irrev one", "bucket": "open_green",
         "two_pass": "PASS", "reversible": False, "pr": 114},
        # Excluded: the auto-merge-clean case the reconciler merges itself.
        {"id": "CLEAN", "title": "auto-merge clean", "bucket": "open_green",
         "two_pass": "PASS", "reversible": True, "operator_merge": False, "pr": 115},
        # Excluded: prose pass-equivalents the normalizer now recognizes — these
        # were the live strings an exact `== "PASS"` match stranded (0 merges for
        # days). They auto-merge-clean now, so they must NOT surface.
        {"id": "SHIPPLAIN", "title": "ship plain", "bucket": "open_green",
         "two_pass": "SHIP", "reversible": True, "pr": 130},
        {"id": "SHIPNBC", "title": "ship non-blocking concerns", "bucket": "open_green",
         "two_pass": "SHIP (2 non-blocking concerns) — independent adversarial reviewer",
         "reversible": True, "pr": 131},
        {"id": "SHIPNOBLK", "title": "ship concerns-no-blockers", "bucket": "open_green",
         "two_pass": "SHIP / CONCERNS-no-blockers", "reversible": True, "pr": 132},
        # Excluded: reversible:false but already MERGED (not an open PR).
        {"id": "MERGEDIRR", "title": "merged irrev", "bucket": "merged",
         "two_pass": "PASS", "reversible": False, "pr": 116},
        # Excluded: still building (dispatched, no PR).
        {"id": "BUILDING", "title": "still building", "bucket": "dispatched",
         "two_pass": "pending", "reversible": True, "pr": None},
        # Excluded: would be Rescue (CONCERNS) but snoozed into the future.
        {"id": "SNZ", "title": "snoozed chip", "bucket": "open_green",
         "two_pass": "CONCERNS", "reversible": True, "pr": 117,
         "snoozed_until": "2099-01-01"},
        # Excluded: terminal bucket — a stale verdict on a merged/done chip is
        # NOT a held merge (the reconciler handles merged+non-PASS live).
        {"id": "MERGEDCONCERNS", "title": "merged with stale concerns",
         "bucket": "merged", "two_pass": "CONCERNS", "reversible": True, "pr": 118},
        {"id": "DONEFAIL", "title": "done with stale fail", "bucket": "done",
         "two_pass": "FAIL", "reversible": True, "pr": 119},
        # Excluded: DRIFTED terminal buckets — the variants real ledgers carry
        # (trailing deploy/verify state, uppercase, close-synonyms). A non-PASS
        # verdict on these is a STALE verdict on already-merged work, NOT a held
        # merge; an exact {merged,live,done} test stranded them all in Rescue.
        {"id": "MERGEDDEP", "title": "merged then deployed", "bucket": "merged-deployed",
         "two_pass": "PASS-WITH-NITS … concerns", "reversible": True, "pr": 120},
        {"id": "MERGEDDEPV", "title": "merged-deployed-verified", "bucket": "merged-deployed-verified",
         "two_pass": "self (build-agent…)", "reversible": True, "pr": 121},
        {"id": "MERGEDUC", "title": "uppercase MERGED", "bucket": "MERGED",
         "two_pass": "CONCERNS", "reversible": True, "pr": 122},
        {"id": "CLOSEDSUP", "title": "closed superseded", "bucket": "closed_superseded",
         "two_pass": "FAIL", "reversible": True, "pr": 123},
        {"id": "DISMISSED", "title": "dismissed", "bucket": "dismissed",
         "two_pass": "CONCERNS", "reversible": True, "pr": 124},
    ],
    "decisions_pending": [
        {"id": "dirfork", "fork": "go A or B?", "recommendation": "A — reason",
         "reversible": True},
        # Excluded: snoozed into the future.
        {"id": "snzd", "fork": "later fork", "recommendation": "x",
         "snoozed_until": "2099-01-01"},
        # Shares an id with an object gate below → the gate is deduped away.
        {"id": "shared-gate-id", "fork": "decision that also has a gate",
         "recommendation": "do X"},
    ],
    "gates": [
        # Deduped: same id as a decisions_pending entry → show only the decision.
        {"id": "shared-gate-id", "desc": "restates the decision",
         "blocked_on": "operator"},
        {"id": "opgate", "desc": "do the operator thing", "blocked_on": "operator"},
        # Excluded: gated on another aspect, not the operator.
        {"id": "platgate", "desc": "platform dep", "blocked_on": "platform"},
    ],
}

LEDGER_EXTRA = {
    "aspect": "extra",
    "updated": "2026-06-14",
    "chips": [
        # (D) Verify-then-merge.
        {"id": "OM1", "title": "operator merge pending click", "bucket": "open_green",
         "two_pass": "PASS", "reversible": True, "operator_merge": True, "pr": 222},
        {"id": "GU1", "title": "green unverified", "bucket": "open_green",
         "two_pass": "pending", "reversible": True, "pr": 223},
    ],
    "gates": [
        "Verify the thing on the live pod",          # string operator note → (C)
        "wait until ≥ 06-30 then do x",              # date threshold → excluded
        "blocked on platform 8.3",                   # another aspect → excluded
        {"id": "snzgate", "desc": "snoozed gate", "blocked_on": "operator",
         "snoozed_until": "2099-01-01"},             # snoozed → excluded
        {"id": "eyeball", "desc": "eyeball both themes",
         "blocked_on": "operator (post-promote eyeball)"},  # operator → (C)
    ],
}


def _write_fixture(tmp_path):
    d = tmp_path / "meta-state"
    d.mkdir()
    (d / "a-core.json").write_text(json.dumps(LEDGER_CORE))
    (d / "b-extra.json").write_text(json.dumps(LEDGER_EXTRA))
    (d / "_README.md").write_text("not a ledger")  # must be ignored
    return d


EXPECTED_TEXT = """\
Queue as of 2026-06-17T09:00:00Z · run /reconcile for live state

(A) Rescue
  #1  [core]  stalled one (PR #111, stalled (dead chip)) — relaunch from last_commit (≤2), or investigate
  #2  [core]  fail one (PR #112, two-pass FAIL) — bounce back to the chip — resolve the FAIL
  #3  [core]  concerns one (PR #113, two-pass CONCERNS) — review the flagged concerns before merging
  #4  [core]  irrev one (PR #114, irreversible (reversible:false)) — auditor-grade human review before merge

(B) Decisions
  #5  [core]  go A or B? — A — reason
  #6  [core]  decision that also has a gate — do X

(C) Gates
  #7  [core]  opgate: do the operator thing — operator action
  #8  [extra]  Verify the thing on the live pod — operator note
  #9  [extra]  eyeball: eyeball both themes — operator action

(D) Verify-then-merge
  #10  [extra]  operator merge pending click (PR #222, verified (PASS) · operator_merge (clearable)) — merge your click — or clear operator_merge to let it auto-merge (reversible + non-privileged: the flag isn't needed here)
  #11  [extra]  green unverified (PR #223, green but unverified) — verify, then merge

11 item(s) need you across 2 aspects.
"""


def test_full_text_projection(tmp_path):
    """The whole rendered queue is pinned — grouping, numbering, dedup, snooze."""
    d = _write_fixture(tmp_path)
    last_seen = tmp_path / "last-seen.json"
    last_seen.write_text(json.dumps({"last_run": "2026-06-17T09:00:00Z"}))
    result = mq.project(d, TODAY, str(last_seen))
    assert mq.render_text(result) == EXPECTED_TEXT


def test_counts_and_group_membership(tmp_path):
    d = _write_fixture(tmp_path)
    result = mq.project(d, TODAY, str(tmp_path / "missing.json"))
    assert result["count"] == 11
    assert result["aspect_count"] == 2
    g = result["groups"]
    assert [it["id"] for it in g["rescue"]] == ["S1", "F1", "C1", "IR1"]
    assert [it["id"] for it in g["decisions"]] == ["dirfork", "shared-gate-id"]
    assert [it["id"] for it in g["gates"]] == ["opgate", None, "eyeball"]
    assert [it["id"] for it in g["verify_merge"]] == ["OM1", "GU1"]


def test_excluded_chips_never_surface(tmp_path):
    """Auto-merge-clean, merged-irreversible, still-building, and snoozed chips
    must NOT appear anywhere in the queue."""
    d = _write_fixture(tmp_path)
    result = mq.project(d, TODAY, str(tmp_path / "missing.json"))
    surfaced = {it["id"] for grp in result["groups"].values() for it in grp}
    for excluded in ("CLEAN", "SHIPPLAIN", "SHIPNBC", "SHIPNOBLK", "MERGEDIRR",
                     "BUILDING", "SNZ", "MERGEDCONCERNS", "DONEFAIL",
                     "MERGEDDEP", "MERGEDDEPV", "MERGEDUC", "CLOSEDSUP", "DISMISSED",
                     "snzd", "snzgate", "platgate"):
        assert excluded not in surfaced
    # shared-gate-id appears as the DECISION, never as the gate.
    gate_ids = [it["id"] for it in result["groups"]["gates"]]
    assert "shared-gate-id" not in gate_ids


def test_freshness_fallback_to_newest_ledger(tmp_path):
    """With no readable last-seen, the stamp falls back to the newest ledger `updated`."""
    d = _write_fixture(tmp_path)
    result = mq.project(d, TODAY, str(tmp_path / "missing.json"))
    assert result["as_of"] == "2026-06-14"
    assert result["as_of_source"] == "ledger"


def test_empty_queue(tmp_path):
    d = tmp_path / "meta-state"
    d.mkdir()
    (d / "quiet.json").write_text(json.dumps(
        {"aspect": "quiet", "updated": "2026-06-18", "chips": [], "gates": [],
         "decisions_pending": []}))
    result = mq.project(d, TODAY, str(tmp_path / "missing.json"))
    assert result["count"] == 0
    text = mq.render_text(result)
    assert "✅ Queue clear — 1 aspects, nothing needs you." in text


# ── unit-level rule pins ─────────────────────────────────────────────────────


def test_snooze_boundary_is_strictly_future():
    """snoozed_until == today is NOT snoozed (only a strictly future date hides)."""
    assert mq._is_snoozed({"snoozed_until": "2099-01-01"}, TODAY) is True
    assert mq._is_snoozed({"snoozed_until": "2026-06-18"}, TODAY) is False  # == today
    assert mq._is_snoozed({"snoozed_until": "2026-06-01"}, TODAY) is False  # past
    assert mq._is_snoozed({}, TODAY) is False
    assert mq._is_snoozed({"snoozed_until": "not-a-date"}, TODAY) is False


def test_classify_chip_precedence():
    # stalled wins even when also reversible:false / has a verdict.
    g, _, _ = mq.classify_chip({"bucket": "stalled", "two_pass": "FAIL",
                                "reversible": False, "pr": 1})
    assert g == "rescue"
    # irreversible only holds on an OPEN pr, not once merged.
    assert mq.classify_chip({"bucket": "merged", "two_pass": "PASS",
                             "reversible": False, "pr": 1}) is None
    # terminal buckets are never a held merge, even with a stale FAIL/CONCERNS.
    assert mq.classify_chip({"bucket": "merged", "two_pass": "CONCERNS",
                             "reversible": True, "pr": 1}) is None
    assert mq.classify_chip({"bucket": "done", "two_pass": "FAIL",
                             "reversible": True, "pr": 1}) is None
    assert mq.classify_chip({"bucket": "live", "two_pass": "FAIL",
                             "reversible": True, "pr": 1}) is None
    # …and the DRIFTED terminal variants the live ledgers actually carry
    # (trailing deploy/verify state, casing, other-terminal synonyms): a
    # merged-days-ago chip with a non-PASS verdict must NOT phantom into Rescue.
    for drifted in ("merged-deployed", "merged-deployed-verified", "MERGED",
                    "landed", "closed_superseded", "closed", "superseded",
                    "dismissed", "investigated-resolved"):
        assert mq.classify_chip({"bucket": drifted, "two_pass": "CONCERNS",
                                 "reversible": True, "pr": 1}) is None, drifted
    # auto-merge-clean → not surfaced.
    assert mq.classify_chip({"bucket": "open_green", "two_pass": "PASS",
                             "reversible": True, "operator_merge": False}) is None
    # operator_merge + green + PASS → verify-then-merge (the click).
    g, _, _ = mq.classify_chip({"bucket": "open_green", "two_pass": "PASS",
                                "reversible": True, "operator_merge": True, "pr": 9})
    assert g == "verify_merge"
    # green + freeform non-PASS two_pass → green-but-unverified.
    g, _, _ = mq.classify_chip({"bucket": "open_green",
                                "two_pass": "n/a (coordinator docs)",
                                "reversible": True, "pr": 9})
    assert g == "verify_merge"


def test_is_terminal_bucket_tolerant_of_vocabulary_drift():
    """`is_terminal_bucket` recognizes the schema canon {merged,live,done} AND the
    drifted variants real ledgers carry — case-insensitively, with the trailing
    deploy/verify/close sub-state on the merged-*/closed-*/superseded-* families.
    These were the values that inflated the live queue 9→16 (the reports
    subscription chips, all MERGED, rendered as phantom two-pass FAILs)."""
    # schema canon
    for b in ("merged", "live", "done"):
        assert mq.is_terminal_bucket(b) is True, b
    # casing + underscore↔hyphen + other-terminal synonyms
    for b in ("MERGED", "Merged", "merged_deployed", "merged-deployed",
              "merged-deployed-verified", "landed", "closed", "closed_superseded",
              "superseded", "dismissed", "investigated-resolved"):
        assert mq.is_terminal_bucket(b) is True, b
    # NOT terminal — still in flight / awaiting the operator.
    for b in ("open_green", "open_red", "draft", "dispatched", "backlog",
              "stalled", "snoozed", "", None):
        assert mq.is_terminal_bucket(b) is False, b
    # a non-terminal bucket that merely SHARES a prefix-ish substring must not
    # be swept in: only the `<term>-` prefix forms are terminal.
    assert mq.is_terminal_bucket("merge-pending") is False
    assert mq.is_terminal_bucket("reopened") is False


def test_two_pass_is_case_insensitive():
    g, _, _ = mq.classify_chip({"bucket": "open_green", "two_pass": "fail",
                                "reversible": True, "pr": 1})
    assert g == "rescue"


def test_operator_gate_filtering():
    assert mq._gate_is_operator_object({"blocked_on": "operator"}) is True
    assert mq._gate_is_operator_object({"blocked_on": "operator (eyeball)"}) is True
    assert mq._gate_is_operator_object({"blocked_on": "platform"}) is False
    assert mq._gate_is_operator_object({"blocked_on": "date≥2026-06-18"}) is False
    # string-gate operator-note heuristic
    assert mq._string_gate_is_operator_note("Verify on the live pod") is True
    assert mq._string_gate_is_operator_note("wait until ≥ 06-30") is False
    assert mq._string_gate_is_operator_note("blocked on platform 8.3") is False


# ── verdict normalization (the canonical pass/blocking predicate) ─────────────
# The whole point of the merge-rule fix: chips record the verdict as free prose,
# not the bare enum, so an exact `== "PASS"` match stranded every "SHIP …" PR
# (0 merges for days). These pin verdict_is_pass / verdict_is_blocking against
# the schema's truth table — and the CONSERVATIVE direction: ambiguous → not-pass.

# (verdict, is_pass, is_blocking) — covers the live strings from the incident.
VERDICT_TRUTH_TABLE = [
    ("PASS", True, False),
    ("pass", True, False),                       # case-insensitive
    ("SHIP", True, False),
    ("SHIP (2 non-blocking concerns) — independent adversarial reviewer…", True, False),
    ("SHIP / CONCERNS-no-blockers…", True, False),
    ("SHIP / CONCERNS-no blockers", True, False),
    ("LGTM, no blockers", True, False),
    ("APPROVED", True, False),                   # "approve" prefix covers "approved"
    ("approve", True, False),
    # not pass, not blocking → merely UNVERIFIED (pending/required/n/a/empty).
    ("required AUDITOR-GRADE human review", False, False),
    ("pending", False, False),
    ("n/a (coordinator docs)", False, False),
    ("", False, False),
    (None, False, False),
    # blocking → reviewer affirmatively said no.
    ("FAIL — bug in X", False, True),
    ("fail", False, True),
    ("CONCERNS: cross-bot leak", False, True),
    # CRITICAL: a verdict that OPENS with a pass token but carries a live blocker
    # is NOT a pass — the prefix alone must never green-light a merge.
    ("SHIP — one blocking issue remains", False, True),
    ("SHIP but blocker found", False, True),
    ("PASS — DO NOT MERGE until the release window", False, True),
    # pass-prefix + an explicit not-done hedge → UNVERIFIED (not pass, not blocking):
    # routes to auto-review, never auto-merge. ("ship" must NOT trip on "wip".)
    ("ship pending QA", False, False),
    ("approve pending more review", False, False),
    ("PASS — WIP, more to come", False, False),
    ("SHIP, TODO: add the test", False, False),
    ("LGTM (tbd: light theme)", False, False),
]


def test_verdict_predicate_truth_table():
    for verdict, exp_pass, exp_block in VERDICT_TRUTH_TABLE:
        assert mq.verdict_is_pass(verdict) is exp_pass, "pass(%r)" % (verdict,)
        assert mq.verdict_is_blocking(verdict) is exp_block, "block(%r)" % (verdict,)


def test_verdict_pass_and_blocking_are_mutually_exclusive():
    """No well-formed verdict is BOTH pass and blocking (the router relies on it)."""
    for verdict, _, _ in VERDICT_TRUTH_TABLE:
        assert not (mq.verdict_is_pass(verdict) and mq.verdict_is_blocking(verdict))


def test_classify_chip_normalized_verdicts():
    """classify_chip routes prose verdicts the same as the bare enum used to."""
    # SHIP-style green verdicts auto-merge-clean → not surfaced (THE fix).
    for tp in ("SHIP", "SHIP (2 non-blocking concerns)", "SHIP / CONCERNS-no-blockers"):
        assert mq.classify_chip({"bucket": "open_green", "two_pass": tp,
                                 "reversible": True, "operator_merge": False}) is None
    # A pass-prefixed verdict hiding a real blocker MUST route to rescue, not merge.
    g, reason, _ = mq.classify_chip({"bucket": "open_green",
                                     "two_pass": "SHIP — one blocking issue",
                                     "reversible": True, "pr": 1})
    assert g == "rescue" and reason == "two-pass CONCERNS"
    # Prose FAIL still routes to rescue with the FAIL reason.
    g, reason, _ = mq.classify_chip({"bucket": "open_green", "two_pass": "FAIL: regression",
                                     "reversible": True, "pr": 1})
    assert g == "rescue" and reason == "two-pass FAIL"
    # Unverified (no PASS to merge on) → green-but-unverified, never auto-merged.
    g, reason, _ = mq.classify_chip({"bucket": "open_green",
                                     "two_pass": "required AUDITOR-GRADE review",
                                     "reversible": True, "pr": 1})
    assert g == "verify_merge" and reason == "green but unverified"


def test_classify_chip_operator_merge_clearable_vs_warranted():
    """operator_merge is clearable on a reversible+non-privileged chip, warranted
    (no advisory) on a privileged or irreversible one."""
    # reversible + non-privileged → clearable advisory (cause #2 of the stall).
    g, reason, rec = mq.classify_chip({"bucket": "open_green", "two_pass": "SHIP",
                                       "reversible": True, "operator_merge": True, "pr": 1})
    assert g == "verify_merge"
    assert "clearable" in reason and "clear operator_merge" in rec
    # privileged → operator_merge is warranted; plain "your click", no advisory.
    g, reason, rec = mq.classify_chip({"bucket": "open_green", "two_pass": "PASS",
                                       "reversible": True, "operator_merge": True,
                                       "privileged": True, "pr": 1})
    assert g == "verify_merge" and "clearable" not in reason
    # irreversible → routed to rescue (auditor look) before the operator_merge branch.
    g, _, _ = mq.classify_chip({"bucket": "open_green", "two_pass": "PASS",
                                "reversible": False, "operator_merge": True, "pr": 1})
    assert g == "rescue"
