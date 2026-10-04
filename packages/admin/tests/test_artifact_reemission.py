"""artifact_reemission — "the same document, written N times" fires, and only then.

Brief: ``internal/dispatch/done/artifact-by-reference-pattern.md`` part 3.
Engine: ``evolve_admin.applications.artifact_reemission``; wiring:
``scanner._conversation_detections``; copy: ``app_promotion.offer_text``.

  1. ``TestRule`` — the pure arithmetic: a planted 3× re-emission (the same
     itinerary, one section rewritten each time) fires; one emission does
     not; two do not; two different documents do not cluster; a paragraph
     below the document floor is not an emission; the window excludes old
     emissions.
  2. ``TestReader`` — the gateway session JSONL reader finds the planted
     emissions and the detection carries counts, hashes and a label but NOT
     the document text; the per-bot switch gates it.
  3. ``TestScannerWiring`` — a real ``scan_workspace_pipeline`` run over a home
     with the planted sessions births a draft whose evidence names the
     detector, and ``offer_text`` on that draft is the operator's sentence.
  4. ``TestOfferCopy`` — the sentence, the count, and the generic pitch for
     every other draft.

MUTATION CHECKED: ``TestRule.test_three_near_duplicates_fire`` goes red when
``cluster_emissions`` stops joining on similarity (exact-only); the no-text
assertion goes red when ``detection_from_match`` adds the text to the
evidence; the offer test goes red when ``_rebuilt_document_offer`` returns "".
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_ADMIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ADMIN_DIR))

from evolve_admin.applications import app_promotion as ap  # noqa: E402
from evolve_admin.applications import artifact_reemission as ar  # noqa: E402
from evolve_admin.applications import scanner as sc  # noqa: E402

BOT = "botx"
UTC = timezone.utc


def _itinerary(day2: str) -> str:
    """A document-shaped assistant output (~1.6k chars) with one variable section."""
    return f"""# Lisbon long weekend

## Day 1 — Friday

- 09:40 depart SFO on TP 218, arrive LIS 06:15 the next morning
- Check in at the hotel in Alfama; walk the Alfama stairs before the heat
- Dinner at a tasca in the Baixa (no reservations, arrive before seven)
- Nightcap at a fado house if the legs still work after the flight

## Day 2 — Saturday

{day2}

## Day 3 — Sunday

Free morning. Afternoon tram 28 loop from Martim Moniz, sunset at the
Miradouro da Senhora do Monte, and a slow dinner in Graça with the
neighbourhood regulars, then an early night before the flight home.

## Practicalities

Lisbon runs on cash in the small places and cards everywhere else; keep
twenty euros of coins for trams and the miradouro kiosks. The metro from the
airport takes forty minutes and costs less than two euros; a taxi is about
twenty and takes half the time at six in the morning. Sunday most museums
are free before two, which is why Belém is on Saturday and the tram loop is
on Sunday. Pack a light layer for the evenings — the river wind is real.

## Eating

Breakfast is a coffee and a pastry standing at the counter, which is faster
and cheaper than the hotel. Lunch is the prato do dia wherever the workers
are eating; dinner reservations only matter on Saturday. Order the grilled
sardines once, the bacalhau twice, and the house wine every time.

## Notes

Bring the good walking shoes — Alfama is all hills and cobbles. Confirmation
for the flight is in the wallet app; the hotel has the passport numbers.
"""


DAY2_VERSIONS = (
    "1. Belém: the monastery at opening, before the tour groups\n"
    "2. Pastéis de Belém — buy a dozen, eat three on the wall by the river\n"
    "3. LX Factory for lunch, then the MAAT museum on the waterfront",
    "Belém first thing: the monastery at opening, then the pastry shop.\n\n"
    "Lunch at LX Factory; MAAT after. Fado in the evening (book ahead).",
    "Belém first thing: the monastery at opening, then the pastry shop.\n\n"
    "Lunch at LX Factory; MAAT after. Dinner in Cais do Sodré, then fado.",
)

OTHER_DOC = """# Letter to the landlord

Dear Ms. Alvarez,

I am writing about the deposit for the flat at number twelve, which under the
lease was due back within thirty days of the end of the tenancy. The tenancy
ended on the first of the month and the inspection found no damage beyond
ordinary wear, as the inventory you countersigned records. It is now six
weeks and the deposit has not arrived. Please return the full amount to the
account on file within seven days. If there is a deduction you believe is
justified, please send the itemised statement the lease requires so that we
can discuss it. I would rather settle this directly than through the deposit
scheme, but I will use it if I have to. Thank you for the two good years in
the flat; the neighbours were kind and the roof, mostly, held.

With regards,
A former tenant
""" * 2


def _em(text: str, *, ts: datetime | None, session: str = "s1") -> ar.Emission | None:
    return ar.emission_from_text(text, ts=ts, session_id=session)


def _today() -> date:
    """Today in UTC — the zone every stamp below uses. ``date.today()`` is the
    LOCAL day, and late in a Pacific evening that is one day behind the UTC
    stamps (the clock-coupling class this repo has hit before)."""
    return datetime.now(UTC).date()


def _ts(days_ago: int, hour: int = 10) -> datetime:
    base = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base - timedelta(days=days_ago)


# ── 1. The rule ──────────────────────────────────────────────────────────────


class TestRule:
    def test_a_paragraph_is_not_an_emission(self):
        assert _em("Sure — day 2 now starts at the monastery. Anything else?", ts=_ts(0)) is None
        assert _em(_itinerary(DAY2_VERSIONS[0]), ts=_ts(0)) is not None

    def test_three_near_duplicates_fire(self):
        ems = [_em(_itinerary(v), ts=_ts(2 - i, 9 + i), session=f"s{i // 2}")
               for i, v in enumerate(DAY2_VERSIONS)]
        matches = ar.detect_reemission(ems, as_of=_today(), tz=UTC)
        assert len(matches) == 1
        m = matches[0]
        assert m.count == 3 and m.exact_duplicates == 0
        assert m.label == "lisbon long weekend"
        assert m.days_seen == 3 and m.window_days == ar.WINDOW_DAYS
        assert m.sessions == ("s0", "s1")
        assert m.chars_avg > ar.MIN_CHARS

    def test_one_emission_does_not_fire(self):
        ems = [_em(_itinerary(DAY2_VERSIONS[0]), ts=_ts(0))]
        assert ar.detect_reemission(ems, as_of=_today(), tz=UTC) == []

    def test_two_emissions_do_not_fire_a_draft_and_a_revision_is_a_conversation(self):
        ems = [_em(_itinerary(DAY2_VERSIONS[0]), ts=_ts(1)),
               _em(_itinerary(DAY2_VERSIONS[1]), ts=_ts(0))]
        assert ar.detect_reemission(ems, as_of=_today(), tz=UTC) == []

    def test_identical_re_emissions_in_one_session_fire_and_count_as_exact(self):
        text = _itinerary(DAY2_VERSIONS[0])
        ems = [_em(text, ts=_ts(0, 9 + i), session="same") for i in range(3)]
        matches = ar.detect_reemission(ems, as_of=_today(), tz=UTC)
        assert len(matches) == 1 and matches[0].exact_duplicates == 2
        assert matches[0].days_seen == 1 and matches[0].sessions == ("same",)

    def test_two_different_documents_do_not_cluster(self):
        ems = [_em(_itinerary(DAY2_VERSIONS[0]), ts=_ts(3)),
               _em(OTHER_DOC, ts=_ts(2)),
               _em(_itinerary(DAY2_VERSIONS[1]), ts=_ts(1)),
               _em(OTHER_DOC, ts=_ts(0))]
        assert ar.detect_reemission(ems, as_of=_today(), tz=UTC) == []
        clusters = ar.cluster_emissions(ems)
        assert sorted(len(c) for c in clusters) == [2, 2]

    def test_the_window_excludes_old_emissions(self):
        ems = [_em(_itinerary(v), ts=_ts(ar.WINDOW_DAYS + 5 - i)) for i, v in enumerate(DAY2_VERSIONS)]
        ems.append(_em(_itinerary(DAY2_VERSIONS[0]), ts=_ts(0)))
        assert ar.detect_reemission(ems, as_of=_today(), tz=UTC) == []
        assert len(ar.detect_reemission(ems, as_of=None)) == 1, "no window ⇒ all four count"

    def test_the_pod_timezone_decides_the_day(self):
        # 03:00 UTC on Aug 28/29/30 is the evening of Aug 27/28/29 in Los
        # Angeles. With the window ending Aug 29: in the pod zone all three
        # are inside it and the cluster fires; read as UTC, the last one is
        # AFTER the window end and only two remain — which is not a habit.
        la = ZoneInfo("America/Los_Angeles")
        ems = [_em(_itinerary(v), ts=datetime(2026, 8, 28, 3, 0, tzinfo=UTC) + timedelta(days=i))
               for i, v in enumerate(DAY2_VERSIONS)]
        assert ar.detect_reemission(ems, as_of=date(2026, 8, 29), tz=UTC) == []
        matches = ar.detect_reemission(ems, as_of=date(2026, 8, 29), tz=la)
        assert len(matches) == 1 and matches[0].count == 3 and matches[0].days_seen == 3

    def test_label_is_the_recurring_request_label_rule_applied_to_the_heading(self):
        # Same normalizer as conversation_recurrence: stopwords dropped, digits
        # gone, at most six content words, order kept — a title, not a sentence.
        assert ar.label_for("# A Very Long Heading With Many Many Words Indeed\n\nbody") == \
            "very long heading many words indeed"
        assert ar.label_for("no heading here, just prose that goes on\nmore") == "heading prose goes"
        assert ar.label_for("# Invoice 4417 for Dr Weinstein, acct 8891\n\nbody") == \
            "invoice weinstein acct"
        assert ar.label_for("# 2026 Q3 — 1234 5678\n\nbody") == "document", "digits-only ⇒ the neutral label"
        assert ar.detection_id_for("lisbon long weekend").startswith("reem-lisbon-long-weekend-")

    def test_a_headingless_letter_does_not_leak_its_opening_line(self):
        body = OTHER_DOC.split("\n", 2)[2].split("# Letter", 1)[0]     # no heading anywhere
        letter = ("Dear Ms. Alvarez, I am writing about the deposit for flat 12 held since "
                  "March 2024 under account 8891.\n" + body)
        assert "#" not in letter
        label = ar.label_for(letter)
        assert label.split()[:4] == ["dear", "alvarez", "writing", "deposit"], label
        assert len(label.split()) <= 6
        assert not any(ch.isdigit() for ch in label), "no flat number, year or account id"
        assert not letter.lower().startswith(label), "not a prefix of the text"


# ── 2. The reader ────────────────────────────────────────────────────────────


def _plant_sessions(home: Path, texts: list[tuple[datetime, str]], *, per_session: int = 2) -> list[str]:
    """Gateway-shaped session files: ``{timestamp, message{role, content[]}}``."""
    sessions_dir = home / ".openclaw" / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    ids: list[str] = []
    for start in range(0, len(texts), per_session):
        sid = str(uuid.uuid4())
        ids.append(sid)
        lines = []
        for ts, text in texts[start:start + per_session]:
            lines.append(json.dumps({
                "timestamp": ts.isoformat().replace("+00:00", "Z"),
                "message": {"role": "user", "content": [{"type": "text", "text": "change day 2"}]},
            }))
            lines.append(json.dumps({
                "timestamp": (ts + timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                "message": {"role": "assistant", "provider": "anthropic",
                            "content": [{"type": "text", "text": text},
                                        {"type": "toolCall", "name": "noop", "arguments": {}}]},
            }))
        lines.append("this line is not json")
        (sessions_dir / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
    return ids


@pytest.fixture
def pod(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
    home = tmp_path / "home"
    home.mkdir()
    return {"shared": shared, "home": home}


class TestReader:
    def test_planted_re_emission_is_detected_without_carrying_text(self, pod):
        _plant_sessions(pod["home"], [(_ts(2 - i, 9 + i), _itinerary(v))
                                      for i, v in enumerate(DAY2_VERSIONS)])
        found = ar.reemission_detections(pod["shared"], BOT, home=pod["home"])
        assert len(found) == 1
        det = found[0]
        assert det.evidence["detector"] == ar.DETECTOR
        assert det.evidence["count"] == 3 and det.evidence["label"] == "lisbon long weekend"
        assert det.evidence["days_seen"] == 3 and det.evidence["window_days"] == ar.WINDOW_DAYS
        assert det.evidence["offer_copy"] == ar.offer_copy(3)
        assert det.name == "Lisbon Long Weekend"
        blob = json.dumps(det.to_dict())
        for phrase in ("Alfama", "monastery", "cash in the small places", "walking shoes"):
            assert phrase not in blob, f"document text leaked into the detection: {phrase}"

    def test_a_single_emission_is_not_detected(self, pod):
        _plant_sessions(pod["home"], [(_ts(0), _itinerary(DAY2_VERSIONS[0]))])
        assert ar.reemission_detections(pod["shared"], BOT, home=pod["home"]) == []

    def test_no_sessions_means_no_evidence_not_an_error(self, pod):
        assert ar.reemission_detections(pod["shared"], BOT, home=pod["home"]) == []
        assert ar.reemission_detections(pod["shared"], BOT, home=pod["home"] / "missing") == []

    def test_an_opted_out_identity_on_the_bot_switches_the_reader_off(self, pod):
        _plant_sessions(pod["home"], [(_ts(2 - i, 9 + i), _itinerary(v))
                                      for i, v in enumerate(DAY2_VERSIONS)])
        rosters = pod["shared"] / "rosters"
        rosters.mkdir()
        (rosters / f"{BOT}.json").write_text(json.dumps({"do_not_track": {"slack:U1": {"since": "2026-08-01"}}}))
        assert ar.reemission_detections(pod["shared"], BOT, home=pod["home"]) == []
        (rosters / f"{BOT}.json").write_text("{not json")     # unreadable ⇒ fail closed
        assert ar.reemission_detections(pod["shared"], BOT, home=pod["home"]) == []
        (rosters / f"{BOT}.json").write_text(json.dumps({"do_not_track": {}}))
        assert len(ar.reemission_detections(pod["shared"], BOT, home=pod["home"])) == 1

    def test_the_cap_is_applied_before_shingles_are_built(self, pod):
        _plant_sessions(pod["home"], [(_ts(2 - i, 9 + i), _itinerary(v))
                                      for i, v in enumerate(DAY2_VERSIONS)])
        kept = ar.emissions_for_home(pod["home"], max_emissions=2)
        assert len(kept) == 2
        assert ar.emissions_for_home(pod["home"], max_emissions=0) == []
        assert [e.ts for e in kept] == sorted(e.ts for e in kept), "the newest two, in order"
        assert kept[-1].ts == max(e.ts for e in ar.emissions_for_home(pod["home"]))

    def test_the_per_bot_switch_gates_the_reader(self, pod):
        _plant_sessions(pod["home"], [(_ts(2 - i, 9 + i), _itinerary(v))
                                      for i, v in enumerate(DAY2_VERSIONS)])
        (pod["shared"] / "network.json").write_text(json.dumps({
            "timezone": "UTC", "bots": {BOT: {"recurringRequestSignal": False}}}))
        assert ar.reemission_detections(pod["shared"], BOT, home=pod["home"]) == []

    def test_the_cli_reports_and_is_read_only(self, pod, capsys):
        _plant_sessions(pod["home"], [(_ts(2 - i, 9 + i), _itinerary(v))
                                      for i, v in enumerate(DAY2_VERSIONS)])
        before = sorted(p.as_posix() for p in pod["home"].rglob("*"))
        rc = ar.main(["--bot", BOT, "--shared-dir", str(pod["shared"]), "--home", str(pod["home"])])
        out = capsys.readouterr().out
        assert rc == 0 and "×3" in out and "rebuild this document 3 times" in out
        assert sorted(p.as_posix() for p in pod["home"].rglob("*")) == before


# ── 3. Scanner wiring ────────────────────────────────────────────────────────


@pytest.fixture
def scan_env(tmp_path, monkeypatch, pod):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manifests = tmp_path / "manifests"
    manifests.mkdir()

    def _must_not_run(app, *a, **k):
        if getattr(app, "conversation_evidence", None) is not None:
            raise AssertionError("an LLM manifest build was reached for a conversation-only detection")
        return sc._stub_manifest(app, BOT)

    monkeypatch.setattr(sc, "_resolve_llm", lambda tier="tier3": ("stub-model", "stub-key"))
    monkeypatch.setattr(sc, "llm_discover_applications", lambda *a, **k: [])
    monkeypatch.setattr(sc, "generate_manifest_for_app", _must_not_run)
    monkeypatch.setattr(sc, "_stamp_app_kind", lambda *a, **k: False)
    # The reader resolves the bot home through the product's resolver; point
    # it at the planted home the way a fixture pod's profile pin would.
    monkeypatch.setattr("evolve_admin.config.bot_home", lambda bot_id, network=None: pod["home"])
    return {"workspace": workspace, "manifests": manifests, **pod}


def _run_scan(env) -> list[dict]:
    sc.scan_workspace_pipeline(
        workspace=env["workspace"], bot_id=BOT, shared_dir=env["shared"], config={},
        use_llm=True, output_dir=env["manifests"], user=BOT,
    )
    written = [p for p in sorted(env["manifests"].glob("*.json")) if not p.name.startswith((".", "_"))]
    return [json.loads(p.read_text()) for p in written]


class TestScannerWiring:
    def test_a_planted_re_emission_births_a_draft_with_the_offer(self, scan_env):
        _plant_sessions(scan_env["home"], [(_ts(2 - i, 9 + i), _itinerary(v))
                                           for i, v in enumerate(DAY2_VERSIONS)])
        written = _run_scan(scan_env)
        assert len(written) == 1, [m.get("name") for m in written]
        draft = written[0]
        assert draft.get("conversation_only") is True
        ev = draft["conversation_evidence"]
        assert ev["detector"] == ar.DETECTOR and ev["count"] == 3
        assert ev["days_seen"] == 3 and ev["window_days"] == ar.WINDOW_DAYS
        assert "monastery" not in json.dumps(draft)
        pitch = ap.offer_text(draft)
        assert pitch.startswith("You've had me rebuild this document 3 times — want me to make it "
                                "something I can edit instead of rewrite?"), pitch

    def test_a_single_emission_births_nothing(self, scan_env):
        _plant_sessions(scan_env["home"], [(_ts(0), _itinerary(DAY2_VERSIONS[0]))])
        assert _run_scan(scan_env) == []

    def test_an_unreadable_session_tree_does_not_kill_the_scan(self, scan_env, monkeypatch):
        reached: list[str] = []

        def _boom(*a, **k):
            reached.append("reader")
            raise OSError("sessions unreadable")
        monkeypatch.setattr(ar, "emissions_for_home", _boom)
        assert _run_scan(scan_env) == []
        assert reached == ["reader"], "the seam was not reached — this proved nothing"


# ── 4. The offer copy ────────────────────────────────────────────────────────


class TestOfferCopy:
    def test_the_sentence_is_the_operators_own_words(self):
        assert ar.offer_copy(4) == ("You've had me rebuild this document 4 times — want me to make "
                                    "it something I can edit instead of rewrite?")

    def test_a_rebuilt_draft_gets_the_sentence_and_keeps_the_name_line(self):
        manifest = {"name": "Lisbon Long Weekend", "description": "Rebuilt document",
                    "conversation_only": True,
                    "conversation_evidence": {"detector": ar.DETECTOR, "count": 5}}
        text = ap.offer_text(manifest)
        assert text.startswith(ar.offer_copy(5))
        assert "I'd call it **Lisbon Long Weekend**" in text
        assert "tell you if it doesn't run" not in text

    def test_every_other_draft_keeps_the_generic_pitch(self):
        manifest = {"name": "Revenue Digest", "description": "Recurring request",
                    "conversation_evidence": {"detector": "conversation_recurrence", "days_seen": 5}}
        text = ap.offer_text(manifest)
        assert "Want me to make it an app?" in text
        assert "rebuild this document" not in text
        assert "rebuild this document" not in ap.offer_text({"name": "x"})
