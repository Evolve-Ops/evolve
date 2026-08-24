"""AL-1.6c wiring — conversation-only evidence enters scanner discovery.

Design: ``internal/design-app-spec-and-discovery-2026-08-15.md`` §7.1
("Conversational evidence ... is a discovered draft even if no file exists
yet"), §7.1a (the arithmetic), §3 (a new detection is born ``draft_id``-only).
Brief: ``internal/build-AL-1.6-draft-identity.md`` §7.1 / §7.3a.

**Why this file is shaped like ``test_al_1_6a_draft_identity.py``.** That file
exists because both mint sites had never run to completion in production, so
"the code looks right" was not evidence. The same argument applies one layer
up here: ``conversation_recurrence.py`` shipped correct and UNWIRED, and the
only thing that proves it is wired is a real ``scan_workspace_pipeline`` run
that reads the manifest back off disk. So the harness is borrowed wholesale —
same three faked LLM seams, everything downstream production code.

Two differences, and both are load-bearing. ``llm_discover_applications``
returns **nothing** here, so every manifest these tests see came from the
conversation path and nowhere else. And ``generate_manifest_for_app`` is
patched to RAISE: a conversation-only detection has no file for it to read,
so calling it would be paying tier3 to invent an implementation that does not
exist. If it fires, these tests fail loudly.

The purpose/fit classifier (``_stamp_app_kind``) is stubbed rather than
forbidden, because a conversation draft is deliberately NOT exempt from it —
it judges on name and prose, which is all such a draft has, and the noise
class AL-1.6c measured on the live pod (``openclaw heartbeat poll``, ×58) is
exactly what that gate is for. ``test_the_purpose_classifier_still_sees_it``
pins that.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_readiness as ar  # noqa: E402
from evolve_admin.applications import conversation_recurrence as cr  # noqa: E402
from evolve_admin.applications import draft_identity as di  # noqa: E402
from evolve_admin.applications import native_write as nw  # noqa: E402
from evolve_admin.applications import scanner as sc  # noqa: E402
from evolve_admin.applications.app_identity import DRAFT_ID_PREFIX  # noqa: E402

BOT = "botx"
LABEL = "revenue digest"
REQUESTER = "slack:U0PRIMARY"
HOUR = 9


# ── The fixture: 5 recurring_request days inside a 10-day window ─────────────

def _days_back(n: int) -> list[str]:
    """The last ``n`` days ending YESTERDAY, pod-local (network.json pins UTC).

    Ends yesterday rather than today so a run that crosses local midnight
    between writing the rows and reading the clock still has every row inside
    the 10-day window — the clock-coupling class this repo has hit before.
    """
    end = date.today() - timedelta(days=1)
    return [(end - timedelta(days=i)).isoformat() for i in range(n)][::-1]


def _write_rows(shared: Path, days, *, label=LABEL, hour=HOUR, bot=BOT) -> None:
    """One ``session_summary`` annotation per day, in the shape the plugin
    writes (``recurringRequest.ts`` stamps ``{label, requester, hour}``)."""
    out = shared / "annotations" / bot
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for day in days:
        lines.append(json.dumps({
            "type": "session_summary",
            "bot_id": bot,
            "ts": f"{day}T{hour:02d}:12:00Z",
            "outcome": "answered",
            "recurring_request": {
                "label": label, "requester": REQUESTER, "hour": hour,
            },
        }))
    (out / "sessions-2026-08.jsonl").write_text("\n".join(lines) + "\n")


class _Env(NamedTuple):
    workspace: Path
    shared: Path
    manifests: Path
    classified: list
    llm_hits: list


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    shared = tmp_path / "shared"
    shared.mkdir()
    # Pin the pod timezone so the row ts -> local-day mapping in the fixture
    # is the identity, rather than depending on where the test runs.
    (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
    manifests = tmp_path / "manifests"
    manifests.mkdir()

    def _must_not_run(app, *a, **k):
        """Forbidden for a CONVERSATION detection; the scanner's own no-LLM
        builder for anything else, so a test may inject a normal LLM hit."""
        if getattr(app, "conversation_evidence", None) is not None:
            raise AssertionError(
                "generate_manifest_for_app was reached for a conversation-only "
                "detection — there is no file here for it to read"
            )
        return sc._stub_manifest(app, BOT)

    # Every manifest the classifier is offered, so a test can assert the
    # conversation draft is NOT exempted from the purpose/fit gate.
    classified: list[dict] = []

    def _classify(manifest, *a, **k):
        classified.append(manifest)
        return False

    # LLM discovery returns nothing BY DEFAULT, so every manifest a test sees
    # came from the conversation path. `llm_hits` is a mutable handle a test
    # can append to — the independent review of this PR found that stubbing
    # this to [] *for every test* hid a real defect, because the interaction
    # between an LLM hit and a conversation detection was never exercised.
    llm_hits: list = []
    monkeypatch.setattr(sc, "_resolve_llm", lambda tier="tier3": ("stub-model", "stub-key"))
    monkeypatch.setattr(sc, "llm_discover_applications", lambda *a, **k: list(llm_hits))
    monkeypatch.setattr(sc, "generate_manifest_for_app", _must_not_run)
    monkeypatch.setattr(sc, "_stamp_app_kind", _classify)

    return _Env(workspace, shared, manifests, classified, llm_hits)


def _run_scan(scan_env) -> list[dict]:
    workspace, shared, manifests, _classified, _hits = scan_env
    sc.scan_workspace_pipeline(
        workspace=workspace, bot_id=BOT, shared_dir=shared, config={},
        use_llm=True, output_dir=manifests, user=BOT,
    )
    written = [p for p in sorted(manifests.glob("*.json"))
               if not p.name.startswith((".", "_"))]
    return [json.loads(p.read_text()) for p in written]


def _gallery_specs(shared: Path) -> list[str]:
    """Every Spec published under ``{shared_dir}/gallery``.

    The manifests dir is NOT a sufficient surface for "was a draft minted?":
    both the L3 sweep and the dedup pass remove a manifest AFTER
    ``mint_scanner_detection`` has already published a Spec (with a durable
    ``p-`` id) and a lessons file, and neither cleanup reclaims those. A test
    that only globs the manifests dir cannot see the leak. Two real defects
    hid behind exactly that blind spot.
    """
    root = shared / "gallery"
    return sorted(p.relative_to(shared).as_posix() for p in root.rglob("*.json")) \
        if root.exists() else []


def _lessons_files(shared: Path) -> list[str]:
    root = shared / "lessons"
    return sorted(p.relative_to(shared).as_posix() for p in root.rglob("*.json")) \
        if root.exists() else []


def _force_legacy_fallback(monkeypatch) -> None:
    """The standing production condition that reaches the legacy write: the
    scanner runs as the bot user and cannot create a Spec under the
    evolve-owned gallery (native_write.convert_scanned_bot_to_v7_arc)."""
    def _denied(*a, **k):
        raise PermissionError("[Errno 13] gallery/local is evolve-owned")
    monkeypatch.setattr(nw, "mint_scanner_detection", _denied)


# ── The proof ────────────────────────────────────────────────────────────────

class TestAScanBirthsAConversationDraft:
    """5-of-10-day rows on disk -> a draft with draft_id, no app_id,
    evidence class conversation-only. Read back off disk, both mint paths."""

    @pytest.mark.parametrize("legacy_path", [False, True])
    def test_a_draft_is_born_draft_id_only(self, scan_env, monkeypatch, legacy_path):
        if legacy_path:
            _force_legacy_fallback(monkeypatch)
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        written = _run_scan(scan_env)

        assert len(written) == 1, "expected exactly one conversation draft"
        data = written[0]
        assert data.get("draft_id", "").startswith(DRAFT_ID_PREFIX)
        assert not data.get("app_id"), (
            "a conversation detection must NOT be born with an app_id — "
            "identity is conferred by promotion, not discovery (design §3)"
        )
        assert di.classify(data)[0] == di.CLASS_DRAFT

    @pytest.mark.parametrize("legacy_path", [False, True])
    def test_the_evidence_class_is_conversation_only(
        self, scan_env, monkeypatch, legacy_path,
    ):
        """Both mint paths must carry the evidence, not just the legacy one.

        The native v7-arc mint drops every field not in
        ``_NATIVE_INSTANCE_PASSTHROUGH``, so without the passthrough entry
        this draft lands on disk with NO evidence at all — indistinguishable
        from an empty stub on the very path that minted it.
        """
        if legacy_path:
            _force_legacy_fallback(monkeypatch)
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        data = _run_scan(scan_env)[0]

        assert data.get("conversation_only") is True
        ev = data.get("conversation_evidence") or {}
        assert ev.get("evidence") == "conversation_only"
        assert ev.get("label") == LABEL
        assert ev.get("days_seen") == 5
        assert ev.get("window_days") == cr.DEFAULT_WINDOW_DAYS
        # The cluster centre is the LOWEST hour covering the most days
        # (documented tie-break), so assert coverage, not equality — every
        # row here sits at the same hour, which ties five candidate centres.
        assert abs(ev.get("center_hour") - HOUR) <= cr.DEFAULT_HOUR_TOLERANCE
        assert ev.get("primary_requester") == REQUESTER

    def test_the_draft_scores_on_the_LOWEST_evidence_rung(self, scan_env):
        """The carrier this chip writes is the one AL-1.6b reads.

        ``app_readiness`` was built tolerant of a ``conversation_only`` field
        that nothing wrote yet. This asserts the two halves actually meet — a
        field-name typo here would leave the draft scoring 0 on evidence and
        nothing else in the suite would notice. No threshold in either module
        is touched or asserted on; only the rung.
        """
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        data = _run_scan(scan_env)[0]
        tiers = ar._evidence_tiers_present(data)

        assert "conversation_only" in tiers
        assert tiers == {"conversation_only"}, (
            "a conversation-only draft must claim no other evidence rung"
        )
        weight = dict((k, w) for k, w, _ in ar.EVIDENCE_TIERS)["conversation_only"]
        assert weight == min(w for _, w, _ in ar.EVIDENCE_TIERS)

    def test_the_scanned_draft_measures_the_recurrence_dimension(self, scan_env):
        """D-R (2026-08-22), end to end on the real scan path.

        AL-1.7 §9.2 measured ``dimensions_measured=1`` on all 74 manifests and
        named the cause: ``recurrence``'s only carrier was
        ``usage_metadata.invocation_count``, whose sole non-test writer copies a
        v13 zero. D-R makes ``conversation_evidence.days_seen / window_days`` —
        design §7.1a's own recurrence rule — that dimension's producer for this
        class of draft. This asserts the two halves meet on a manifest the
        SCANNER wrote, not on a fixture: a key renamed on either side would
        silently drop this back to one dimension.

        No threshold in either module is asserted on or touched.

        MUTATION CHECKED: deleting the ``conversation_evidence`` branch in
        ``app_readiness._recurrence_dimension`` reds this.
        """
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        data = _run_scan(scan_env)[0]
        readiness = ar.score_readiness(data)
        recurrence = next(d for d in readiness.dimensions if d.key == "recurrence")

        assert recurrence.measured is True, data.get("conversation_evidence")
        assert recurrence.score == 5 / cr.DEFAULT_WINDOW_DAYS
        assert readiness.dimensions_measured == 2, (
            "evidence + recurrence — the number AL-1.7 §9.2 measured as 1"
        )
        # The other half of D-R: no run count was invented for this draft.
        assert not (data.get("usage_metadata") or {}).get("invocation_count")

    def test_no_files_are_claimed(self, scan_env):
        """"No file exists yet" IS this evidence class. A draft that claimed
        files would climb to the ``observed_files`` rung on evidence it does
        not have."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        data = _run_scan(scan_env)[0]

        assert not data.get("evidence_files")
        assert not data.get("files")


    def test_the_purpose_classifier_still_sees_it(self, scan_env):
        """A conversation draft is NOT exempt from the purpose/fit gate.

        AL-1.6c's own measurement found the most-repeated recurring labels on
        the live pod are system noise (``openclaw heartbeat poll``, ×58). The
        classifier judges on name + prose, which is exactly what a
        conversation draft has, so exempting it would remove the one gate that
        can tell that class apart from a real habit.
        """
        workspace, shared, manifests, classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        _run_scan(scan_env)

        assert any(m.get("conversation_only") is True for m in classified), (
            "the conversation draft never reached the purpose/fit classifier"
        )


class TestTheThresholdIsHonoured:
    """The gate is design §7.1a's, unchanged, and it is really applied."""

    def test_four_days_of_ten_births_nothing(self, scan_env):
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(4))
        assert _run_scan(scan_env) == []

    def test_rows_outside_the_window_do_not_count(self, scan_env):
        """Five days, all of them 40 days old. The habit stopped; the window
        is anchored on TODAY, not on the newest row, so nothing is drafted."""
        workspace, shared, manifests, _classified, _hits = scan_env
        old = date.today() - timedelta(days=40)
        _write_rows(shared, [(old - timedelta(days=i)).isoformat() for i in range(5)])
        assert _run_scan(scan_env) == []

    def test_a_chatty_afternoon_is_not_a_habit(self, scan_env):
        """Ten asks on ONE day. §7.1a counts days, not occurrences."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, [_days_back(1)[0]] * 10)
        assert _run_scan(scan_env) == []


class TestTheScanIsUnaffectedWhenThereIsNothingToSee:

    def test_no_annotations_at_all(self, scan_env):
        assert _run_scan(scan_env) == []

    def test_an_unreadable_detector_does_not_kill_the_scan(
        self, scan_env, monkeypatch,
    ):
        """A monitor that dies on one bad read reports nothing about the rest
        of the scan. The adapter swallows and logs."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        monkeypatch.setattr(
            cr, "conversation_detections",
            lambda *a, **k: (_ for _ in ()).throw(OSError("annotations unreadable")),
        )
        assert _run_scan(scan_env) == []


class TestDoNotTrackIsHonouredOnTheREADSide:
    """Both gates the in-bot side applies at WRITE time, applied again at
    READ time — the per-bot switch and the per-identity roster exclusion.

    Flipping either off stops new rows but leaves up to a window's worth on
    disk, and a draft minted from those is exactly the observation that was
    just switched off. The in-bot gate alone leaves that window open.
    """

    def test_the_per_bot_flag_suppresses_the_draft(self, scan_env):
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        (shared / "network.json").write_text(json.dumps({
            "timezone": "UTC",
            "bots": {BOT: {"recurringRequestSignal": False}},
        }))
        assert _run_scan(scan_env) == []

    def test_the_detector_gates_itself_not_only_the_scanner(self, tmp_path):
        """The switch is enforced in TWO places and each needs its own test.

        `conversation_detections` checks it, and `scanner._conversation_
        detections` checks it again so the scan log can name which zero it is.
        Defense in depth is right, but it means a scan-level test passes with
        the detector's own gate deleted — proved by mutation. Any other
        caller of the detector (the CLI, a future generator) is protected by
        this one, so it is tested here directly rather than through a scan.
        """
        shared = tmp_path / "s"
        ann = shared / "annotations" / BOT
        ann.mkdir(parents=True)
        _write_rows(shared, _days_back(5))
        (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
        assert cr.conversation_detections(shared, BOT), "fixture is not producing a match"

        (shared / "network.json").write_text(json.dumps({
            "timezone": "UTC",
            "bots": {BOT: {"recurringRequestSignal": False}},
        }))
        assert cr.conversation_detections(shared, BOT) == []

    def test_only_an_explicit_false_disables(self, tmp_path):
        """Same fail-open shape as ``recurringRequest.ts``: a typo must never
        silently switch observation off."""
        shared = tmp_path / "s"
        shared.mkdir()
        for cfg, expected in (
            ({}, True),
            ({"bots": {}}, True),
            ({"bots": {BOT: {}}}, True),
            ({"bots": {BOT: {"recurringRequestSignal": True}}}, True),
            ({"bots": {BOT: {"recurringRequestSignal": "false"}}}, True),
            ({"bots": {BOT: {"recurringRequestSignal": False}}}, False),
        ):
            (shared / "network.json").write_text(json.dumps(cfg))
            assert cr.recurring_request_signal_enabled(shared, BOT) is expected, cfg

    def test_an_opted_out_requester_does_not_build_a_draft(self, scan_env):
        """Per-identity do-not-track, honoured on the READ side.

        The bot's gate stops NEW rows; a person who opts out today has up to
        a window's worth already on disk, and a draft minted from those is
        the observation they just opted out of.
        """
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        rosters = shared / "rosters"
        rosters.mkdir()
        (rosters / f"{BOT}.json").write_text(json.dumps({
            "do_not_track": {REQUESTER: {"set_at": "2026-08-20"}},
        }))
        assert _run_scan(scan_env) == []

    def test_a_blocked_requester_does_not_build_a_draft(self, scan_env):
        """A blocked identity's traffic must never become evidence for an
        app draft — the same rule the in-bot gate applies."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        rosters = shared / "rosters"
        rosters.mkdir()
        (rosters / f"{BOT}.json").write_text(json.dumps({
            "blocked": {REQUESTER: {"reason": "spam"}},
        }))
        assert _run_scan(scan_env) == []

    def test_an_unrelated_exclusion_does_not_suppress_the_draft(self, scan_env):
        """The filter must key on the requester, not merely on the overlay
        existing — an over-broad read would silently switch the whole
        evidence class off on any bot with a populated roster."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        rosters = shared / "rosters"
        rosters.mkdir()
        (rosters / f"{BOT}.json").write_text(json.dumps({
            "do_not_track": {"slack:USOMEONEELSE": {}},
            "blocked": {"discord:U9": {}},
        }))
        assert len(_run_scan(scan_env)) == 1

    def test_the_exclusion_key_space_matches_what_the_bot_writes(self):
        """``buildRecurringRequest`` writes ``requester`` as
        ``platform:senderId``, which is the overlay's own key space. If these
        two ever diverge the filter reads a key that is never present and
        fails silently OPEN — the worst direction for a privacy gate."""
        assert REQUESTER == "slack:U0PRIMARY"
        assert ":" in REQUESTER

    def test_no_overlay_means_nobody_opted_out(self, tmp_path):
        """A fresh bot has no overlay. Matches the TS side's
        ``if (!overlay) return false``."""
        shared = tmp_path / "s"
        (shared / "rosters").mkdir(parents=True)
        assert cr.excluded_requesters(shared, BOT) == frozenset()

    def test_an_UNREADABLE_overlay_fails_CLOSED(self, tmp_path):
        """The TS side fails open here and this deliberately does not.

        A fail-open in the bot decides whether to write one row, and the row
        ages out of a ten-day window. A fail-open on the pod side mints a
        manifest AND a gallery Spec with a durable ``p-`` id, which never ages
        out. Same policy, permanently different consequence — and skipping one
        scan of an evidence source costs nothing, because the next scan
        re-reads it. Raised by the independent review of this PR.
        """
        shared = tmp_path / "s"
        (shared / "rosters").mkdir(parents=True)
        (shared / "rosters" / f"{BOT}.json").write_text("{not json")
        assert cr.excluded_requesters(shared, BOT) is None
        (shared / "rosters" / f"{BOT}.json").write_text('["a list, not an object"]')
        assert cr.excluded_requesters(shared, BOT) is None

    def test_a_PRESENT_but_unreadable_overlay_is_none(self, tmp_path):
        """The `except OSError` branch, which the malformed-JSON test does
        NOT reach — it exercises `except (JSONDecodeError, ValueError)`.

        Round 2 of the independent review showed that reverting
        `except OSError: return None` to `return frozenset()` left the suite
        green, so the real-world case (EACCES — a roster written by another
        user, which is the normal ownership on a pod) silently failed OPEN
        again, the exact direction ruling 6 was about.

        A directory at the overlay path raises `IsADirectoryError`: an
        `OSError` that is NOT `FileNotFoundError`, so it takes the same
        branch a permission error takes — and needs no root to set up.
        """
        shared = tmp_path / "s"
        (shared / "rosters").mkdir(parents=True)
        (shared / "rosters" / f"{BOT}.json").mkdir()
        assert cr.excluded_requesters(shared, BOT) is None

    def test_an_unreadable_overlay_draws_no_drafts(self, scan_env):
        """The fail-closed decision, at the level that matters — the scan."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        (shared / "rosters").mkdir()
        (shared / "rosters" / f"{BOT}.json").write_text("{not json")
        assert _run_scan(scan_env) == []
        assert _gallery_specs(shared) == []

    def test_both_overlay_spellings_are_read(self, tmp_path):
        shared = tmp_path / "s"
        (shared / "rosters").mkdir(parents=True)
        (shared / "rosters" / f"{BOT}.json").write_text(json.dumps({
            "doNotTrack": {"slack:UCAMEL": {}},
            "do_not_track": {"slack:USNAKE": {}},
            "blocked": {"slack:UBLOCK": {}},
        }))
        assert cr.excluded_requesters(shared, BOT) == frozenset(
            {"slack:UCAMEL", "slack:USNAKE", "slack:UBLOCK"}
        )

    def test_another_bots_flag_does_not_leak(self, tmp_path):
        shared = tmp_path / "s"
        shared.mkdir()
        (shared / "network.json").write_text(json.dumps({
            "bots": {"other": {"recurringRequestSignal": False}},
        }))
        assert cr.recurring_request_signal_enabled(shared, BOT) is True


class TestItDoesNotDuplicateAnAppTheScannerAlreadyFound:

    def test_an_existing_manifest_for_the_same_app_is_not_re_drafted(
        self, scan_env,
    ):
        """The habit is real AND the workspace already has the app. One
        manifest, and it is the existing one — the conversation detection
        must not mint a parallel draft beside it.

        The GALLERY assertion is the one that pins Phase 3b's match, and it
        is not incidental. Removing the match leaves the outcome on disk
        unchanged — the dedup pass merges the two manifests by name a few
        phases later, so the manifests dir alone cannot tell the two
        implementations apart. What it cannot undo is that the draft was
        MINTED first: ``native_write`` publishes a Spec carrying a durable
        ``p-`` id at discovery (the §7.3a finding, reported and unfixed), so
        a detection that should never have been drafted leaves a permanent
        gallery entry for an app that already exists. Matching first is what
        stops that; dedup is only the second net.
        """
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        (manifests / "revenue-digest.json").write_text(json.dumps({
            "id": "revenue-digest",
            "name": "Revenue Digest",
            "bot_id": BOT,
            "app_id": "revenue-digest",
            "definition_status": "defined",
            "files": [{"path": "scripts/digest.py", "layer": "code"}],
        }))

        written = _run_scan(scan_env)

        assert len(written) == 1
        assert written[0].get("app_id") == "revenue-digest", (
            "the pre-existing defined app was overwritten"
        )
        assert not written[0].get("conversation_only")
        assert list((shared / "gallery").rglob("*.json")) == [], (
            "a Spec was published for a draft that should never have been "
            "minted — the Phase 3b existing-manifest match did not run"
        )


class TestTheExistingL3GuardsStillDiscriminate:
    """The noise class AL-1.6c measured on the live pod, run through the
    scanner's existing sweep.

    ``_archive_platform_file_only_stubs`` Rule 4 archives a manifest that
    realizes nothing, claims recurring behavior, AND carries an infra signal.
    A conversation draft is the first detection class that meets the first
    two by construction — so the third is what does the work, and it does it
    with no new guard: ``openclaw heartbeat poll`` (×58 in the AL-1.6c
    measurement) is swept, ``revenue digest`` is kept.
    """

    def test_an_infra_noise_label_is_never_drafted(self, scan_env):
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5), label="openclaw heartbeat poll")
        assert _run_scan(scan_env) == []

    @pytest.mark.parametrize("label", [
        # Trips `_describes_pod_infra` only — name tokens are
        # {openclaw, heartbeat, poll}, 1 of 3 OC, NOT a majority.
        "openclaw heartbeat poll",
        # Trips `_name_is_oc_system_function` only — {session, startup} is
        # 2 of 2, a majority, and none of the six `_INFRA_PURPOSE_PATTERNS`
        # literals appear in the text.
        "session startup",
    ])
    def test_it_is_dropped_BEFORE_the_mint_not_archived_after_it(
        self, scan_env, label,
    ):
        """The defect the independent review of this PR found, pinned.

        BOTH halves of the pre-mint filter are parametrized, because round 2
        of that review showed the original single case exercised only one of
        them: deleting `_name_is_oc_system_function` left the suite green
        while an entire label family (`session startup`, `memory
        persistence`, every `_OC_SYSTEM_FUNCTION_TOKENS` majority) went back
        to being minted-then-archived, i.e. finding 1 reopened and unpinned.

        Rule 4 recognises this shape but runs AFTER Phase 4, and the mint has
        by then published a Spec with a durable ``p-`` id plus a lessons file
        that the archive does not reclaim. ``_stub_manifest`` sets no
        ``pkg_id``, so each scan mints a FRESH random ``p-``. An hourly
        heartbeat label clears 5-of-10 days trivially, so before the fix three
        scans deposited three orphan Specs and three lessons files — one per
        scan, forever, on the label AL-1.6c measured ×58 on the live pod.

        Three scans, because one cannot tell "cleaned up" from "never made".
        """
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5), label=label)

        for _ in range(3):
            assert _run_scan(scan_env) == []

        assert _gallery_specs(shared) == [], (
            "an infra-shaped conversation label was minted before it was "
            "archived, orphaning its Spec in the gallery"
        )
        assert _lessons_files(shared) == []

    def test_a_real_habit_with_the_same_shape_is_kept(self, scan_env):
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))
        assert len(_run_scan(scan_env)) == 1

    def test_a_kept_draft_mints_exactly_one_spec_across_rescans(self, scan_env):
        """The other half of the leak: a draft that IS kept must reuse its
        Spec, not mint a fresh ``p-`` on every scan."""
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        for _ in range(3):
            assert len(_run_scan(scan_env)) == 1

        assert len(_gallery_specs(shared)) == 1, _gallery_specs(shared)


    def test_an_LLM_hit_for_the_same_app_does_not_defeat_the_match(
        self, scan_env,
    ):
        """Finding 2 of the independent review, pinned.

        The common case for a REAL habit is that it also has files, so the
        LLM detects it in the same scan. Phase 3b originally passed
        ``claimed_paths`` into the match — and the LLM loop had already put
        that manifest in it, so the conversation detection found no match
        exactly when the app most obviously existed. It drafted, dedup merged
        the manifest away, and the Spec stayed orphaned in the gallery.

        Every other test in this file stubs ``llm_discover_applications`` to
        ``[]``, which is why none of them could see this.
        """
        workspace, shared, manifests, _classified, hits = scan_env
        _write_rows(shared, _days_back(5))
        (manifests / "revenue-digest.json").write_text(json.dumps({
            "id": "revenue-digest",
            "name": "Revenue Digest",
            "bot_id": BOT,
            "app_id": "revenue-digest",
            "definition_status": "defined",
            "files": [{"path": "scripts/digest.py", "layer": "code"}],
        }))
        hits.append(sc.DetectedApplication(
            id="revenue-digest", name="Revenue Digest", confidence=0.9,
            evidence_files=["scripts/digest.py"], evidence_summary="a cron runs it",
            suggested_goals=[], suggested_tests=[], suggested_privacy=[],
            description="Sorts revenue nightly.",
        ))

        written = _run_scan(scan_env)

        assert len(written) == 1
        assert written[0].get("app_id") == "revenue-digest"
        assert not written[0].get("conversation_only")
        assert _gallery_specs(shared) == [], (
            "the conversation detection was drafted beside an app the LLM had "
            "just claimed; dedup hid the manifest but not the Spec"
        )

    def test_an_LLM_hit_under_a_DIFFERENT_name_still_leaves_the_match_working(
        self, scan_env,
    ):
        """The case that isolates the empty-``claimed`` match.

        Two guards now cover finding 2: matching with an empty ``claimed``
        set, and a canonical-name check against the LLM's hits. They overlap
        whenever the LLM's name resembles the habit's — so neither mutation
        showed up until this case, where they do NOT overlap. The LLM
        re-clusters the app under "Daily Revenue Report" and claims the
        manifest by EVIDENCE overlap; the canon check therefore sees nothing
        familiar, and only the existing-manifest match can stop the draft.
        """
        workspace, shared, manifests, _classified, hits = scan_env
        _write_rows(shared, _days_back(5))
        (manifests / "revenue-digest.json").write_text(json.dumps({
            "id": "revenue-digest",
            "name": "Revenue Digest",
            "bot_id": BOT,
            "app_id": "revenue-digest",
            "definition_status": "defined",
            "evidence_files": ["scripts/digest.py", "scripts/rollup.py"],
            "files": [{"path": "scripts/digest.py", "layer": "code"}],
        }))
        hits.append(sc.DetectedApplication(
            id="daily-revenue-report", name="Daily Revenue Report", confidence=0.9,
            evidence_files=["scripts/digest.py", "scripts/rollup.py"],
            evidence_summary="a cron runs it", suggested_goals=[],
            suggested_tests=[], suggested_privacy=[],
            description="Rolls up revenue nightly.",
        ))

        written = _run_scan(scan_env)

        assert len(written) == 1, [w.get("name") for w in written]
        assert not any(w.get("conversation_only") for w in written)
        assert _gallery_specs(shared) == []

    def test_an_LLM_hit_for_a_NEW_app_of_the_same_name_does_not_double_draft(
        self, scan_env,
    ):
        """The other half: the LLM finds the same app but it has no manifest
        yet, so both detections are 'new'. One app, one draft."""
        workspace, shared, manifests, _classified, hits = scan_env
        _write_rows(shared, _days_back(5))
        hits.append(sc.DetectedApplication(
            id="revenue-digest", name="Revenue Digest", confidence=0.9,
            evidence_files=["scripts/digest.py"], evidence_summary="a cron runs it",
            suggested_goals=[], suggested_tests=[], suggested_privacy=[],
            description="Sorts revenue nightly.",
        ))

        written = _run_scan(scan_env)

        assert len(written) == 1
        assert len(_gallery_specs(shared)) == 1


class TestTheDetectionIdIsStable:
    """The stem is what the next scan re-matches on AND what seeds the
    ``draft_id``. A stem that churned would mint a second draft for the same
    habit on every scan."""

    def test_a_rescan_does_not_mint_a_second_draft(self, scan_env):
        workspace, shared, manifests, _classified, _hits = scan_env
        _write_rows(shared, _days_back(5))

        first = _run_scan(scan_env)
        second = _run_scan(scan_env)

        assert len(second) == 1
        assert second[0]["draft_id"] == first[0]["draft_id"]

    def test_the_id_is_derived_from_the_label_and_is_deterministic(self):
        assert cr.detection_id_for(LABEL) == cr.detection_id_for(LABEL)
        assert cr.detection_id_for(LABEL).startswith(cr.DETECTION_ID_PREFIX)
        assert cr.detection_id_for("revenue digest") != cr.detection_id_for("morning brief")

    def test_a_long_label_stays_within_the_id_length_rule(self):
        long_label = "extraordinarily verbose reconciliation summarization digest compilation"
        got = cr.detection_id_for(long_label)
        assert len(got) <= 48
        assert got == cr.detection_id_for(long_label)
        assert got != cr.detection_id_for(long_label + " variant")

    def test_a_label_that_slugifies_to_nothing_still_yields_an_id(self):
        assert cr.detection_id_for("") == f"{cr.DETECTION_ID_PREFIX}request"


class TestTheDetectionAdapterIsPure:

    def test_confidence_is_days_over_window_not_an_llm_score(self):
        match = cr.RecurrenceMatch(
            label=LABEL, bot_id=BOT, days_seen=5, window_days=10, occurrences=7,
            first_day="2026-08-11", last_day="2026-08-20", center_hour=9,
            hour_spread=1, primary_requester=REQUESTER, requesters=(REQUESTER,),
        )
        det = cr.detection_from_match(match)
        assert det.confidence == 0.5
        assert det.evidence["evidence"] == "conversation_only"
        assert det.name == "Revenue Digest"

    def test_the_description_says_why_the_draft_exists(self):
        match = cr.RecurrenceMatch(
            label=LABEL, bot_id=BOT, days_seen=7, window_days=10, occurrences=9,
            first_day="2026-08-11", last_day="2026-08-20", center_hour=9,
            hour_spread=2, primary_requester=REQUESTER, requesters=(REQUESTER,),
        )
        det = cr.detection_from_match(match)
        assert "7 of the last 10 days" in det.description
        assert "09:00" in det.description
