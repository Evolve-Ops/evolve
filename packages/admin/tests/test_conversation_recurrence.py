"""Tests for conversation-only evidence — the pod-side half (AL-1.6c).

Design: internal/design-app-spec-and-discovery-2026-08-15.md §7.1a.

Three groups:

1. **The cross-language pin.** The same fixture the node test asserts
   against, at ``packages/plugin/tests/fixtures/recurring-request-labels.json``.
   Here the vocabularies are compared as SETS (Python can, node cannot
   without exporting them), so a token added on one side and not the other
   fails loudly instead of silently halving the detector's recall.

2. **The arithmetic.** §7.1a's rule is "same normalized label on >=N of the
   last M days (start 5/10), hour within +/-2h". Every clause is pinned,
   including the two that are easy to get wrong: distinct DAYS rather than
   occurrences, and a CIRCULAR hour distance so a before-bed ask spanning
   midnight still clusters.

3. **Fail-closed reading and memory-stated recurrence.**
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from evolve_admin.applications import conversation_recurrence as cr

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "packages" / "plugin" / "tests" / "fixtures"
    / "recurring-request-labels.json"
)


@pytest.fixture(scope="module")
def fixture() -> dict:
    assert FIXTURE_PATH.is_file(), f"shared fixture missing: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text())


# ── 1. the cross-language pin ────────────────────────────────────────────────

def test_fixture_pins_constants(fixture):
    c = fixture["constants"]
    assert cr.MAX_LABEL_TOKENS == c["max_label_tokens"]
    assert cr.MIN_LABEL_TOKENS == c["min_label_tokens"]
    assert cr.MAX_LABEL_TAGS == c["max_label_tags"]
    assert cr.MAX_SCAN_CHARS == c["max_scan_chars"]


def test_fixture_pins_vocabularies_exactly(fixture):
    """Set equality, not sampling.

    This is the assertion that actually holds the two implementations
    together. The node test can only probe tokens through the normalizer;
    this one compares the lists themselves.
    """
    v = fixture["vocabulary"]
    assert sorted(cr.VOLATILE_TOKENS) == v["volatile_tokens"]
    assert sorted(cr.STOPWORDS) == v["stopwords"]
    assert sorted(cr.SYNTHETIC_PREFIXES) == v["synthetic_prefixes"]
    assert sorted(cr.SENTINEL_TEXTS) == v["sentinel_texts"]


def test_every_fixture_normalize_case_matches(fixture):
    cases = fixture["normalize_cases"]
    assert len(cases) >= 20, "the shared fixture must not shrink"
    for case in cases:
        assert cr.normalize_request_label(case["input"]) == case["expected"], case["name"]


def test_every_fixture_compose_case_matches(fixture):
    for case in fixture["compose_cases"]:
        assert cr.compose_label(case["head"], case["tags"]) == case["expected"], case["name"]


def test_no_digit_survives_into_a_label():
    """The privacy contract: the label cannot carry a number."""
    probes = [
        "send account 4111111111111111 summary report",
        "call me on 555-0134 about the revenue digest",
        "user42 wants the revenue digest",
        "revenue digest for Q3 2026 at 09:30",
    ]
    for p in probes:
        label = cr.normalize_request_label(p)
        # Assert the probe KEYS first: guarded by `if label is not None`
        # alone, a regression to all-null would turn this green while
        # asserting nothing.
        assert label is not None, f"probe stopped keying, test would be vacuous: {p}"
        assert not any(ch.isdigit() for ch in label), label


def test_no_digit_survives_into_a_composed_label():
    """The contract is about the EMITTED label, not just the head.

    Tags are operator configuration, but they are concatenated into the
    label that leaves the bot, so they are sanitized too.
    """
    label = cr.compose_label("morning summary", ["invoice-4111111111111111", "email"])
    assert not any(ch.isdigit() for ch in label), label


def test_composed_label_length_is_bounded():
    label = cr.compose_label("morning summary", ["x" * 5000, "y" * 5000])
    assert len(label) <= len("morning summary") + 2 + 2 * (cr.MAX_TAG_CHARS + 3)


def test_sanitize_tag_rejects_content_free_input():
    assert cr.sanitize_tag("") is None
    assert cr.sanitize_tag(None) is None
    assert cr.sanitize_tag("2026") is None
    assert cr.sanitize_tag("  --  ") is None
    assert cr.sanitize_tag("Health-Nutrition") == "health-nutrition"


# ── 2. the arithmetic ────────────────────────────────────────────────────────

def _row(day, *, label="morning summary", hour=9, requester="telegram:99", bot="bot-a"):
    return cr.RecurrenceRow(label=label, requester=requester, hour=hour,
                            day=day, bot_id=bot)


DAYS = [f"2026-08-{d:02d}" for d in range(10, 20)]  # 10 consecutive days


def test_threshold_is_at_least_min_days():
    """Exactly N days matches; N-1 does not. The boundary is the whole rule."""
    five = [_row(d) for d in DAYS[:5]]
    assert len(cr.detect_recurrence(five, as_of=DAYS[-1])) == 1

    four = [_row(d) for d in DAYS[:4]]
    assert cr.detect_recurrence(four, as_of=DAYS[-1]) == []


def test_same_day_repeats_count_once():
    """One chatty afternoon must not fake ten days of habit."""
    rows = [_row(DAYS[0]) for _ in range(20)]
    assert cr.detect_recurrence(rows, as_of=DAYS[-1]) == []


def test_hour_tolerance_is_plus_or_minus_two():
    on_time = [_row(d, hour=h) for d, h in zip(DAYS[:5], [9, 10, 11, 8, 7])]
    assert len(cr.detect_recurrence(on_time, as_of=DAYS[-1])) == 1

    # Spread the same five asks over 12 hours: no 5-hour-wide cluster exists.
    scattered = [_row(d, hour=h) for d, h in zip(DAYS[:5], [1, 6, 11, 16, 21])]
    assert cr.detect_recurrence(scattered, as_of=DAYS[-1]) == []


def test_hour_distance_is_circular():
    """23:00 and 01:00 are two hours apart, not twenty-two.

    Without this the 'every night before bed' app — a class §7.1a exists
    to catch — could never accumulate.
    """
    assert cr.circular_hour_distance(23, 1) == 2
    assert cr.circular_hour_distance(1, 23) == 2
    assert cr.circular_hour_distance(0, 12) == 12
    assert cr.circular_hour_distance(9, 9) == 0

    rows = [_row(d, hour=h) for d, h in zip(DAYS[:5], [23, 0, 1, 23, 0])]
    matches = cr.detect_recurrence(rows, as_of=DAYS[-1])
    assert len(matches) == 1
    assert cr.circular_hour_distance(matches[0].center_hour, 0) <= 1


def test_window_excludes_older_rows():
    old = [_row(f"2026-07-{d:02d}") for d in range(1, 9)]
    recent = [_row(d) for d in DAYS[:3]]
    assert cr.detect_recurrence(old + recent, as_of=DAYS[-1]) == []
    # Widening the window past the old rows brings them back.
    assert len(cr.detect_recurrence(old + recent, as_of=DAYS[-1], window_days=60)) == 1


def test_distinct_labels_do_not_merge():
    a = [_row(d, label="morning summary") for d in DAYS[:5]]
    b = [_row(d, label="evening digest") for d in DAYS[:3]]
    matches = cr.detect_recurrence(a + b, as_of=DAYS[-1])
    assert [m.label for m in matches] == ["morning summary"]


def test_distinct_bots_do_not_merge():
    a = [_row(d, bot="bot-a") for d in DAYS[:5]]
    b = [_row(d, bot="bot-b") for d in DAYS[:5]]
    matches = cr.detect_recurrence(a + b, as_of=DAYS[-1])
    assert sorted(m.bot_id for m in matches) == ["bot-a", "bot-b"]


def test_secondary_requesters_still_count_as_evidence():
    """§7.2: 'a secondary user's recurring requests still count as evidence'."""
    rows = [_row(d, requester="telegram:99") for d in DAYS[:3]]
    rows += [_row(d, requester="telegram:77") for d in DAYS[3:5]]
    matches = cr.detect_recurrence(rows, as_of=DAYS[-1])
    assert len(matches) == 1
    assert matches[0].days_seen == 5
    assert matches[0].primary_requester == "telegram:99"  # most frequent
    assert matches[0].requesters == ("telegram:77", "telegram:99")  # sorted


def test_primary_requester_tie_breaks_alphabetically():
    """The offer must name the same person on every run."""
    rows = [_row(d, requester="telegram:99") for d in DAYS[:3]]
    rows += [_row(d, requester="telegram:11") for d in DAYS[3:6]]
    m = cr.detect_recurrence(rows, as_of=DAYS[-1])[0]
    assert m.primary_requester == "telegram:11"


def test_hour_cluster_ties_resolve_to_the_lowest_hour():
    """The docstring claims ties resolve to the lowest hour; pin it.

    With every ask at exactly 09:00 and a tolerance of +/-2, all of centres
    7..11 cover the same 5 days — a five-way tie. Only the strict `>` in
    the centre scan keeps the FIRST (lowest) candidate. Mutation-checked:
    `>` -> `>=` turns this red, and it previously survived the whole suite.
    """
    rows = [_row(d, hour=9) for d in DAYS[:5]]
    m = cr.detect_recurrence(rows, as_of=DAYS[-1])[0]
    assert m.center_hour == 7


def test_result_is_order_independent():
    rows = [_row(d, hour=h) for d, h in zip(DAYS, [9, 10, 8, 9, 11, 9, 7, 10, 9, 8])]
    baseline = cr.detect_recurrence(rows, as_of=DAYS[-1])
    rng = random.Random(20260819)
    for _ in range(20):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        assert cr.detect_recurrence(shuffled, as_of=DAYS[-1]) == baseline


def test_match_carries_the_conversation_only_evidence_class():
    m = cr.detect_recurrence([_row(d) for d in DAYS[:5]], as_of=DAYS[-1])[0]
    assert m.evidence == "conversation_only"
    assert m.window_days == cr.DEFAULT_WINDOW_DAYS
    assert m.first_day == DAYS[0]
    assert m.last_day == DAYS[4]
    assert m.to_dict()["requesters"] == ["telegram:99"]


def test_empty_and_malformed_input_yields_no_matches():
    assert cr.detect_recurrence([]) == []
    assert cr.detect_recurrence([_row("not-a-date")]) == []
    assert cr.detect_recurrence([_row(DAYS[0])], as_of="garbage") == []


# ── 3. reading rows off disk ─────────────────────────────────────────────────

def _write_annotations(shared: Path, bot: str, records: list[dict], day="2026-08-19"):
    d = shared / "annotations" / bot
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _summary(ts, label="morning summary", requester="telegram:99", hour=9, bot="bot-a"):
    return {
        "type": "session_summary", "bot_id": bot, "ts": ts,
        "recurring_request": {"label": label, "requester": requester, "hour": hour},
    }


def test_iter_reads_only_well_formed_recurring_request_rows(tmp_path):
    shared = tmp_path / "shared"
    (shared).mkdir()
    (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
    _write_annotations(shared, "bot-a", [
        _summary("2026-08-19T09:05:00Z"),
        {"type": "turn_annotation", "recurring_request": {
            "label": "x y", "requester": "a:1", "hour": 9}},          # wrong type
        {"type": "session_summary", "bot_id": "bot-a",
         "ts": "2026-08-19T09:05:00Z"},                                # no field
        _summary("2026-08-19T09:05:00Z", hour=99),                     # bad hour
        _summary("2026-08-19T09:05:00Z", label=""),                    # empty label
        _summary("2026-08-19T09:05:00Z", requester=""),                # no requester
        _summary("not-a-timestamp"),                                   # bad ts
    ])
    # A corrupt line must not stop the read.
    with (shared / "annotations" / "bot-a" / "2026-08-19.jsonl").open("a") as fh:
        fh.write('{"type": "session_summary", "recurring_request": {oops\n')
    _write_annotations(shared, "bot-a", [_summary("2026-08-19T10:05:00Z")])

    rows = list(cr.iter_recurrence_rows(shared))
    assert len(rows) == 2
    assert {r.hour for r in rows} == {9}
    assert {r.day for r in rows} == {"2026-08-19"}
    assert {r.bot_id for r in rows} == {"bot-a"}


def test_iter_uses_pod_local_day_not_utc(tmp_path):
    """A 23:30 Los Angeles ask is still 'that day', not the next one."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "network.json").write_text(json.dumps({"timezone": "America/Los_Angeles"}))
    # 2026-08-20T05:30Z == 2026-08-19 22:30 PDT.
    _write_annotations(shared, "bot-a", [_summary("2026-08-20T05:30:00Z", hour=22)],
                       day="2026-08-20")
    rows = list(cr.iter_recurrence_rows(shared))
    assert [r.day for r in rows] == ["2026-08-19"]


def test_iter_skips_cost_event_files_and_missing_dirs(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    d = shared / "annotations" / "bot-a"
    d.mkdir(parents=True)
    (d / "cost_events-2026-08-19.jsonl").write_text(
        json.dumps(_summary("2026-08-19T09:00:00Z")) + "\n"
    )
    assert list(cr.iter_recurrence_rows(shared)) == []
    # No annotations dir at all is not an error.
    assert list(cr.iter_recurrence_rows(tmp_path / "nope")) == []


def test_iter_can_restrict_to_one_bot(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _write_annotations(shared, "bot-a", [_summary("2026-08-19T09:00:00Z", bot="bot-a")])
    _write_annotations(shared, "bot-b", [_summary("2026-08-19T09:00:00Z", bot="bot-b")])
    assert {r.bot_id for r in cr.iter_recurrence_rows(shared)} == {"bot-a", "bot-b"}
    assert {r.bot_id for r in cr.iter_recurrence_rows(shared, bot_id="bot-a")} == {"bot-a"}


def test_detect_from_shared_dir_end_to_end(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
    for day in DAYS[:6]:
        _write_annotations(shared, "bot-a", [_summary(f"{day}T09:10:00Z")], day=day)
    matches = cr.detect_from_shared_dir(shared, as_of=DAYS[-1])
    assert len(matches) == 1
    assert matches[0].days_seen == 6
    assert matches[0].evidence == "conversation_only"


def test_timezone_falls_back_when_network_is_unusable(tmp_path):
    assert cr.resolve_pod_timezone(tmp_path).key == cr.DEFAULT_TZ_NAME
    (tmp_path / "network.json").write_text("{not json")
    assert cr.resolve_pod_timezone(tmp_path).key == cr.DEFAULT_TZ_NAME
    (tmp_path / "network.json").write_text(json.dumps({"timezone": "Not/AZone"}))
    assert cr.resolve_pod_timezone(tmp_path).key == cr.DEFAULT_TZ_NAME


# ── 4. memory-stated recurrence (§7.1a third bullet) ─────────────────────────

def test_memory_statement_needs_both_a_request_and_a_cadence():
    hits = cr.scan_memory_text(
        "Pat asks for a morning summary every weekday.", source="MEMORY.md"
    )
    assert len(hits) == 1
    assert hits[0].evidence == "memory_stated"
    assert hits[0].cadence == "every weekday"
    assert hits[0].source == "MEMORY.md"
    assert "summary" in hits[0].label


def test_cadence_alone_is_not_evidence():
    """'the backup runs daily' is infrastructure, not a conversational app.

    Requiring both halves is what keeps this detector from firing on every
    operational note in MEMORY.md — the most likely way it would have
    manufactured drafts wholesale.
    """
    for text in [
        "The backup runs daily at 3am.",
        "Deploys happen every weekday.",
        "Nightly the log rotates.",
    ]:
        assert cr.scan_memory_text(text) == [], text


def test_request_alone_is_not_evidence():
    for text in [
        "Pat asks for a summary of the incident.",
        "Robin wants the quarterly numbers.",
    ]:
        assert cr.scan_memory_text(text) == [], text


def test_longest_cadence_phrase_wins():
    hits = cr.scan_memory_text("Robin asks for the standup notes every weekday morning.")
    assert hits[0].cadence == "every weekday morning"


def test_memory_hits_are_de_duplicated():
    text = (
        "Pat asks for a morning summary every weekday.\n"
        "Pat asks for a morning summary every weekday.\n"
    )
    assert len(cr.scan_memory_text(text)) == 1


def test_memory_bullet_markup_is_stripped():
    hits = cr.scan_memory_text("- [x] Pat asks for the revenue digest every morning")
    assert len(hits) == 1
    assert not hits[0].label.startswith("-")


def test_memory_label_equals_the_conversational_key():
    """A memory-stated habit and an observed one must collide, not fork.

    EQUALITY, not a substring/subset relation. ``detect_recurrence`` groups
    on the exact ``(bot_id, label)`` tuple, so anything weaker than equality
    means the two paths produce two drafts for one habit — the opposite of
    what §7.1a's third bullet is for. An earlier version of this test
    asserted a three-way OR and passed while the labels genuinely differed
    ('pat asks revenue digest morning' vs 'revenue digest').
    """
    stated = cr.scan_memory_text("Pat asks for the revenue digest every morning")[0]
    observed = cr.normalize_request_label("give me the revenue digest")
    assert observed is not None
    assert stated.label == observed


@pytest.mark.parametrize("sentence", [
    "Robin checks in every day",
    "Pat asks every morning",
    "Robin requests daily",
    "Robin wants nightly",
])
def test_empty_request_span_fails_closed_and_never_leaks_a_name(sentence):
    """An ordinary MEMORY.md line naming no artifact must yield NOTHING.

    These sentences have a request verb and a cadence but nothing between
    them. An earlier fix fell back to the whole sentence here, emitting
    labels like 'robin checks day' — leaking the real name that
    ``_request_span`` exists to remove, for precisely the lines whose
    label is worthless. Empty span means no evidence.

    Mutation-checked: restoring the `or low` fallback turns this red.
    """
    assert cr.scan_memory_text(sentence) == [], sentence
    assert cr._request_span(sentence.lower(),
                            next(p for p in cr._REQUEST_PHRASES
                                 if p in sentence.lower()),
                            next(c for c in cr._CADENCE_PHRASES
                                 if c in sentence.lower())) == ""


@pytest.mark.parametrize("sentence", [
    "Pat asks for the revenue digest every morning",
    "Every morning Pat asks for the revenue digest",
    "- [x] Robin asks me for the revenue digest each day",
    "The user requests the revenue digest daily",
])
def test_memory_label_is_the_request_not_the_sentence(sentence):
    """The label is the asked-for THING, with the asker's name cut out.

    A person's name has no business in a key that leaves the bot, and
    keeping it would also break the equality above.
    """
    hits = cr.scan_memory_text(sentence)
    assert hits, sentence
    assert hits[0].label == "revenue digest", (sentence, hits[0].label)
    for name in ("pat", "robin", "asks", "requests"):
        assert name not in hits[0].label.split()


def test_memory_and_observed_evidence_merge_into_one_match():
    """End-to-end: 4 observed days + 1 memory-stated day == a 5-day match.

    This is the property the module claims and the reviewer falsified on
    the previous implementation, where the two labels never merged and
    this returned no match at all.
    """
    stated = cr.scan_memory_text("Pat asks for the revenue digest every morning")[0]
    rows = [_row(d, label="revenue digest") for d in DAYS[:4]]
    rows.append(_row(DAYS[4], label=stated.label))
    matches = cr.detect_recurrence(rows, as_of=DAYS[-1])
    assert len(matches) == 1
    assert matches[0].days_seen == 5
    assert matches[0].label == "revenue digest"


def test_scan_memory_text_tolerates_non_text():
    assert cr.scan_memory_text(None) == []
    assert cr.scan_memory_text("") == []
    assert cr.scan_memory_text(b"bytes") == []


def test_scan_memory_files_honours_the_size_floor(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "memory").mkdir(parents=True)
    stub = "Pat asks for a morning summary every weekday."
    assert len(stub) < cr._MIN_MEMORY_BYTES
    (ws / "MEMORY.md").write_text(stub)                       # below floor → skipped
    (ws / "memory" / "log.md").write_text(stub + " " + "padding. " * 40)
    hits = cr.scan_memory_files(ws)
    assert len(hits) == 1
    assert hits[0].source.endswith("memory/log.md")


def test_scan_memory_files_on_a_missing_workspace(tmp_path):
    assert cr.scan_memory_files(tmp_path / "nope") == []


# ── 5. the CLI states its population (anti-vacuity) ──────────────────────────

def test_cli_prints_the_population_even_with_zero_matches(tmp_path, capsys):
    shared = tmp_path / "shared"
    shared.mkdir()
    rc = cr.main(["--shared-dir", str(shared)])
    out = capsys.readouterr().out
    assert rc == 0
    # A detector reporting zero against an unstated population says nothing.
    assert "population: 0 recurring_request rows" in out
    assert "no conversation-only recurrence above threshold" in out


def test_cli_json_reports_population_and_matches(tmp_path, capsys):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
    for day in DAYS[:5]:
        _write_annotations(shared, "bot-a", [_summary(f"{day}T09:10:00Z")], day=day)
    rc = cr.main(["--shared-dir", str(shared), "--as-of", DAYS[-1], "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["population"]["rows_read"] == 5
    assert payload["population"]["bots"] == ["bot-a"]
    assert payload["thresholds"] == {
        "min_days": 5, "window_days": 10, "hour_tolerance": 2
    }
    assert len(payload["matches"]) == 1
    assert payload["matches"][0]["evidence"] == "conversation_only"


def test_cli_window_anchors_to_today_not_to_the_newest_row(tmp_path, capsys):
    """A habit that stopped months ago must not read as current.

    detect_recurrence stays clock-free (it defaults to the newest row);
    main() supplies today so the operator-facing surface measures the last
    N days, not the last N days *of data*.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "network.json").write_text(json.dumps({"timezone": "UTC"}))
    for day in [f"2026-05-{d:02d}" for d in range(1, 6)]:
        _write_annotations(shared, "bot-a", [_summary(f"{day}T09:10:00Z")], day=day)

    # Clock-free core, anchored at the data: reports a match.
    rows = list(cr.iter_recurrence_rows(shared))
    assert len(cr.detect_recurrence(rows)) == 1

    # The CLI with NO --as-of must anchor at today and report none.
    # Passing --as-of here would exercise "an explicit anchor is honored",
    # which was true before the fix too — the test would survive reverting
    # it. Mutation-checked: reverting `args.as_of or <today>` to
    # `args.as_of` turns this red.
    rc = cr.main(["--shared-dir", str(shared), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    # Today in the POD timezone, which is what the CLI anchors to. Using
    # the laptop-local date.today() here would flap across the UTC/PDT
    # boundary — a clock-coupled test bug, not a real failure.
    # Two clock reads (the CLI's and this one) straddle pod midnight once a
    # day, so accept either side of the rollover rather than flaking
    # overnight. The assertion that matters is the second one.
    now = datetime.now(cr.resolve_pod_timezone(shared)).date()
    assert payload["as_of"] in {
        now.isoformat(), (now - timedelta(days=1)).isoformat(),
    }
    # Not anchored at the newest ROW (2026-05-05) — this is the fix.
    assert payload["as_of"] > "2026-05-05"
    assert payload["matches"] == []
    assert payload["population"]["rows_read"] == 5   # population still stated


# ── 6. the do-not-track gates on the CLI (the measurement surface) ───────────
#
# The bug these pin: `main()` read the same evidence store as
# `conversation_detections()` and honoured NEITHER gate, so a pod whose
# operator had switched observation off still had its users' habits — and
# their identities — printed on the operator surface. Arrived with #3726,
# found by the independent review of #3746.
#
# The shape of the fix is a judgement, not just a filter, so these pin the
# judgement too: the RAW population must stay honest (a gated zero must not
# read as "no data"), the withheld count must be VISIBLE (a silent gate is
# indistinguishable from an absent one), and the exclusion must name a
# reason class and never an identity.

def _network(shared: Path, **bots) -> None:
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "network.json").write_text(json.dumps({
        "timezone": "UTC",
        "bots": {b: {"recurringRequestSignal": v} for b, v in bots.items()},
    }))


def _roster(shared: Path, bot: str, **blocks) -> None:
    d = shared / "rosters"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{bot}.json").write_text(json.dumps(
        {k: {who: {} for who in v} for k, v in blocks.items()}
    ))


@pytest.fixture
def pod(tmp_path) -> Path:
    """Six days of two labels from two requesters, on two bots.

    ``telegram:77``'s label is the one the gates must be able to remove,
    and it clears the threshold on its own so removing it is a real change
    in the reported result rather than a cosmetic one.
    """
    shared = tmp_path / "shared"
    _network(shared)
    for bot in ("bot-a", "bot-b"):
        for day in DAYS[:6]:
            _write_annotations(shared, bot, [
                _summary(f"{day}T09:10:00Z", bot=bot),
                _summary(f"{day}T09:20:00Z", label="evening digest",
                         requester="telegram:77", bot=bot),
            ], day=day)
    return shared


def _cli(shared: Path, capsys, *extra) -> str:
    rc = cr.main(["--shared-dir", str(shared), "--as-of", DAYS[-1], *extra])
    assert rc == 0
    return capsys.readouterr().out


def _cli_json(shared: Path, capsys, *extra) -> dict:
    return json.loads(_cli(shared, capsys, "--json", *extra))


class TestTheGateChokepoint:
    """``apply_do_not_track_gates`` — the ONE place both read paths gate."""

    def test_the_per_bot_flag_withholds_that_bots_rows(self, pod):
        _network(pod, **{"bot-b": False})
        kept, gate = cr.apply_do_not_track_gates(
            pod, cr.iter_recurrence_rows(pod))
        assert gate.rows_in == 24
        assert {r.bot_id for r in kept} == {"bot-a"}
        assert [(e.bot_id, e.reason, e.rows_excluded) for e in gate.exclusions] == [
            ("bot-b", cr.GATE_SIGNAL_DISABLED, 12)
        ]

    def test_an_excluded_requester_drops_rows_not_matches(self, pod):
        """The row goes, so the label never accumulates.

        Filtering matches instead would leave the evidence standing and
        merely hide the name on it.
        """
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        kept, gate = cr.apply_do_not_track_gates(
            pod, cr.iter_recurrence_rows(pod))
        assert {r.requester for r in kept if r.bot_id == "bot-a"} == {"telegram:99"}
        assert "evening digest" not in {
            r.label for r in kept if r.bot_id == "bot-a"}
        # bot-b has no overlay, so its rows are untouched — the gate is
        # per-bot and must not leak across.
        assert len([r for r in kept if r.bot_id == "bot-b"]) == 12
        assert [(e.bot_id, e.reason, e.rows_excluded) for e in gate.exclusions] == [
            ("bot-a", cr.GATE_EXCLUDED_REQUESTER, 6)
        ]

    def test_a_blocked_requester_is_excluded_too(self, pod):
        _roster(pod, "bot-a", blocked=["telegram:77"])
        kept, _ = cr.apply_do_not_track_gates(pod, cr.iter_recurrence_rows(pod))
        assert "telegram:77" not in {r.requester for r in kept if r.bot_id == "bot-a"}

    def test_an_unreadable_overlay_withholds_the_whole_bot(self, pod):
        (pod / "rosters").mkdir(parents=True, exist_ok=True)
        (pod / "rosters" / "bot-a.json").write_text("{ not json")
        kept, gate = cr.apply_do_not_track_gates(pod, cr.iter_recurrence_rows(pod))
        assert {r.bot_id for r in kept} == {"bot-b"}
        assert gate.exclusions[0].reason == cr.GATE_OVERLAY_UNREADABLE

    def test_a_row_naming_no_bot_cannot_be_gated_so_it_is_withheld(self, pod):
        """An ungatable row must not become evidence.

        Neither gate is keyed on anything but a bot id, so a row without one
        would sail past both. Fail closed, the same ruling as the unreadable
        overlay.
        """
        rows = [cr.RecurrenceRow(label="x", requester="telegram:1", hour=9,
                                 day=DAYS[0], bot_id="")]
        kept, gate = cr.apply_do_not_track_gates(pod, rows)
        assert kept == []
        assert gate.exclusions[0].reason == cr.GATE_UNATTRIBUTED

    def test_the_all_clear_still_reports_the_population_it_gated(self, pod):
        kept, gate = cr.apply_do_not_track_gates(pod, cr.iter_recurrence_rows(pod))
        assert len(kept) == 24
        assert (gate.rows_in, gate.rows_kept, gate.rows_excluded) == (24, 24, 0)
        assert gate.exclusions == ()

    def test_an_explicit_bot_id_pins_the_gate_key(self, pod):
        """What the scanner path passes.

        It has already restricted the read to one bot's annotation dir;
        ``RecurrenceRow.bot_id`` is the bot's own self-report from inside the
        record, so the caller's key wins when given.
        """
        _network(pod, **{"bot-a": False})
        rows = [cr.RecurrenceRow(label="x", requester="telegram:1", hour=9,
                                 day=DAYS[0], bot_id="bot-b")]
        assert cr.apply_do_not_track_gates(pod, rows)[0] == rows      # per-row key
        assert cr.apply_do_not_track_gates(pod, rows, bot_id="bot-a")[0] == []

    def test_the_reason_detail_never_names_an_identity(self, pod):
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        _, gate = cr.apply_do_not_track_gates(pod, cr.iter_recurrence_rows(pod))
        blob = json.dumps(gate.to_dict())
        assert "telegram:77" not in blob
        assert "rows_excluded" in blob    # the count IS reported

    def test_the_scanner_path_routes_through_the_same_gate(self, pod):
        """Class assertion: one gate, not two implementations of one policy.

        The bug was two read paths over one store with the gates on one of
        them. Pin that they now agree, by making the CLI's gated row set and
        the scanner seam's detections move together on the same input.
        """
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        labels = {d.evidence["label"]
                  for d in cr.conversation_detections(pod, "bot-a", as_of=DAYS[-1])}
        kept, _ = cr.apply_do_not_track_gates(
            pod, cr.iter_recurrence_rows(pod, bot_id="bot-a"), bot_id="bot-a")
        assert labels == {"morning summary"}
        assert "evening digest" not in {r.label for r in kept}


class TestTheCLIHonoursTheGates:

    def test_an_opted_out_requesters_habit_is_not_reported(self, pod, capsys):
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        out = _cli(pod, capsys, "--bot", "bot-a")
        assert "morning summary" in out
        assert "evening digest" not in out      # the ungated CLI printed this
        assert "telegram:77" not in out         # …and named them

    def test_the_per_bot_flag_suppresses_that_bots_matches(self, pod, capsys):
        _network(pod, **{"bot-b": False, "bot-a": True})
        payload = _cli_json(pod, capsys)
        assert {m["bot_id"] for m in payload["matches"]} == {"bot-a"}
        assert payload["do_not_track"]["rows_excluded"] == 12

    def test_the_population_stays_RAW_when_rows_are_withheld(self, pod, capsys):
        """The honest-zero contract.

        A gated population would render "the operator switched observation
        off" as "there is no data" — the exact green-nothing reading the
        population line exists to prevent. Mutation-checked: pointing
        `rows` at the gated set turns this red.
        """
        _network(pod, **{"bot-a": False, "bot-b": False})
        payload = _cli_json(pod, capsys)
        assert payload["population"]["rows_read"] == 24
        assert payload["population"]["gated"] is False
        assert payload["population"]["bots"] == ["bot-a", "bot-b"]
        assert payload["matches"] == []                       # …and zero evidence
        assert payload["do_not_track"]["rows_excluded"] == 24

    def test_a_gated_zero_is_legible_as_a_finding_not_as_no_data(self, pod, capsys):
        _network(pod, **{"bot-a": False, "bot-b": False})
        out = _cli(pod, capsys)
        assert "population: 24 recurring_request rows" in out
        assert "24 of 24 rows withheld" in out
        assert "recurringRequestSignal = false" in out
        assert "no conversation-only recurrence above threshold" in out

    def test_the_gate_reports_itself_even_when_it_withholds_nothing(self, pod, capsys):
        """A silent gate reads exactly like an absent one — which it was."""
        out = _cli(pod, capsys)
        assert "do-not-track: 0 of 24 rows withheld (both gates applied)" in out
        assert _cli_json(pod, capsys)["do_not_track"] == {
            "rows_in": 24, "rows_kept": 24, "rows_excluded": 0, "exclusions": [],
        }

    def test_the_exclusion_line_states_a_reason_class_not_an_identity(
        self, pod, capsys
    ):
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        out = _cli(pod, capsys, "--bot", "bot-a")
        assert "[bot-a] 6 rows — requester listed in rosters/bot-a.json" in out
        assert "telegram:77" not in out


class TestRequesterIdentitiesAreRedactedByDefault:
    """Measurement needs the requester COUNT; the identities are downstream.

    Printed by default they also hand an operator the diff — this list
    against the roster — that recovers who is on do_not_track.
    """

    def test_human_output_shows_a_count_not_a_name(self, pod, capsys):
        out = _cli(pod, capsys, "--bot", "bot-a")
        assert "requesters=1 (redacted; --show-requesters)" in out
        assert "telegram:99" not in out

    def test_json_output_redacts_and_says_so(self, pod, capsys):
        payload = _cli_json(pod, capsys, "--bot", "bot-a")
        assert payload["requesters_redacted"] is True
        m = next(m for m in payload["matches"] if m["label"] == "morning summary")
        assert m["primary_requester"] is None
        assert m["requesters"] == []
        assert m["requester_count"] == 1        # the shape survives redaction
        assert m["requesters_redacted"] is True
        assert "telegram:99" not in json.dumps(payload)

    def test_show_requesters_opts_back_in(self, pod, capsys):
        out = _cli(pod, capsys, "--bot", "bot-a", "--show-requesters")
        assert "requester=telegram:99" in out
        payload = _cli_json(pod, capsys, "--bot", "bot-a", "--show-requesters")
        assert payload["requesters_redacted"] is False
        m = next(m for m in payload["matches"] if m["label"] == "morning summary")
        assert m["primary_requester"] == "telegram:99"
        assert m["requesters"] == ["telegram:99"]
        assert "requesters_redacted" not in m

    def test_the_scanner_seam_is_NOT_redacted(self, pod):
        """Redaction is the CLI's, not the module's.

        §7.2's promotion offer names a secondary requester by design
        ("Maria's been asking for this most mornings"), so the detection the
        scanner consumes must keep the field. Redacting at the dataclass
        would have broken that quietly.
        """
        det = cr.conversation_detections(pod, "bot-a", as_of=DAYS[-1])
        assert {d.evidence["primary_requester"] for d in det} == {
            "telegram:99", "telegram:77"}


class TestTheMemoryScanIsGatedWhenItCanBe:
    """§7.1a's third bullet is evidence about the same habit.

    ``--workspace`` is a raw path, so the per-bot switch is only readable
    when ``--bot`` names the owning bot. Unattributed, the scan runs and
    SAYS it ran ungated rather than claiming a gate it could not evaluate.
    """

    @pytest.fixture
    def workspace(self, tmp_path) -> Path:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "MEMORY.md").write_text(
            "Pat asks for a morning summary every weekday. " + "padding. " * 40
        )
        return ws

    def test_the_flag_suppresses_the_memory_scan(self, pod, workspace, capsys):
        _network(pod, **{"bot-a": False})
        payload = _cli_json(pod, capsys, "--bot", "bot-a",
                            "--workspace", str(workspace))
        assert payload["memory_stated"] == []
        assert payload["memory_scan"].startswith("suppressed")

    def test_the_flag_on_lets_it_through(self, pod, workspace, capsys):
        payload = _cli_json(pod, capsys, "--bot", "bot-a",
                            "--workspace", str(workspace))
        assert len(payload["memory_stated"]) == 1
        assert payload["memory_scan"].startswith("gated")

    def test_without_a_bot_the_scan_is_REFUSED_not_run_ungated(
        self, pod, workspace, capsys
    ):
        """An ungatable scan is refused, exactly as an ungatable row is withheld.

        The first revision ran it and merely said so; the independent review
        was right that this answered the same question two ways in one file.
        """
        out = _cli(pod, capsys, "--workspace", str(workspace))
        assert "memory scan: skipped" in out
        assert "pass --bot" in out
        assert "morning summary" not in out.split("memory scan:")[1]
        assert _cli_json(pod, capsys, "--workspace",
                         str(workspace))["memory_stated"] == []


# ── 7. what the independent review of this PR found (all reproduced) ─────────

class TestEveryReadPathGates:
    """The invariant the first revision asserted and did not hold.

    ``detect_from_shared_dir`` shipped exported and ungated, 180 lines above
    the chokepoint, while the module docstring said "two read paths, one
    gate". The rule with no exceptions: anything taking a ``shared_dir`` and
    returning evidence gates.
    """

    def test_detect_from_shared_dir_honours_the_requester_opt_out(self, pod):
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        found = cr.detect_from_shared_dir(pod, bot_id="bot-a", as_of=DAYS[-1])
        assert {m.label for m in found} == {"morning summary"}
        assert "telegram:77" not in {m.primary_requester for m in found}

    def test_detect_from_shared_dir_honours_the_per_bot_flag(self, pod):
        _network(pod, **{"bot-a": False})
        assert cr.detect_from_shared_dir(pod, bot_id="bot-a", as_of=DAYS[-1]) == []

    def test_the_three_shared_dir_readers_agree(self, pod, capsys):
        """One policy, three callers — pinned against each other.

        Two of the three were ungated at some point in this module's life;
        asserting they merely 'apply the gate' would not have caught either.
        """
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        direct = {m.label for m in
                  cr.detect_from_shared_dir(pod, bot_id="bot-a", as_of=DAYS[-1])}
        seam = {d.evidence["label"] for d in
                cr.conversation_detections(pod, "bot-a", as_of=DAYS[-1])}
        cli = {m["label"] for m in
               _cli_json(pod, capsys, "--bot", "bot-a")["matches"]}
        assert direct == seam == cli == {"morning summary"}

    def test_the_reader_is_the_unguarded_primitive_and_says_so(self, pod):
        """R3 — "no exceptions" was itself an over-claim.

        `iter_recurrence_rows` is exported, takes a `shared_dir`, and yields
        rows still carrying `requester`, ungated. That is correct — gating
        is what its callers do with what it yields — but the docstring named
        only one exception when there are two.
        """
        _roster(pod, "bot-a", do_not_track=["telegram:77"])
        raw = {r.requester for r in cr.iter_recurrence_rows(pod, bot_id="bot-a")}
        assert "telegram:77" in raw
        doc = cr.__doc__ or ""
        assert "iter_recurrence_rows" in doc and "detect_recurrence" in doc

    def test_detect_recurrence_itself_stays_ungated_and_disk_free(self, pod):
        """The escape hatch is the PURE function, and that is the point.

        ``detect_recurrence`` takes rows and touches no disk, so it cannot
        gate and must not pretend to. Gating lives at the shared_dir
        boundary; this pins that the split is deliberate.
        """
        rows = [_row(d, requester="telegram:77") for d in DAYS[:5]]
        _network(pod, **{"bot-a": False})
        assert len(cr.detect_recurrence(rows, as_of=DAYS[-1])) == 1


class TestTheGateKeyIsFailClosedOverEveryCandidateBot:
    """A record must not be able to shop for a bot whose switch is still on."""

    def _cross(self, shared: Path, *, dir_bot: str, claims: str) -> None:
        for day in DAYS[:6]:
            _write_annotations(shared, dir_bot,
                               [_summary(f"{day}T09:10:00Z", bot=claims)], day=day)

    def test_a_row_self_reporting_another_bot_cannot_escape_its_dirs_switch(
        self, tmp_path
    ):
        """Measured, not theorised — this leaked 6 of 6 rows.

        `SessionSummarizer` takes the id from `config.botId`, so the two
        agree in practice; keying on the self-report alone made the
        disagreement a fail-OPEN in a module that fails closed four times.
        """
        shared = tmp_path / "shared"
        _network(shared, **{"bot-b": False})
        self._cross(shared, dir_bot="bot-b", claims="bot-a")
        kept, gate = cr.apply_do_not_track_gates(
            shared, cr.iter_recurrence_rows(shared))
        assert kept == []
        assert gate.exclusions[0].bot_id == "bot-b"      # the DIRECTORY, reported
        assert gate.exclusions[0].reason == cr.GATE_SIGNAL_DISABLED

    def test_the_claimed_bots_switch_also_withholds(self, tmp_path):
        """Either key withholding withholds — not just the directory."""
        shared = tmp_path / "shared"
        _network(shared, **{"bot-a": False})
        self._cross(shared, dir_bot="bot-b", claims="bot-a")
        kept, gate = cr.apply_do_not_track_gates(
            shared, cr.iter_recurrence_rows(shared))
        assert kept == []
        assert gate.exclusions[0].bot_id == "bot-a"      # the CLAIM, reported

    def test_agreeing_keys_are_read_once_and_pass(self, tmp_path):
        shared = tmp_path / "shared"
        _network(shared)
        self._cross(shared, dir_bot="bot-a", claims="bot-a")
        kept, gate = cr.apply_do_not_track_gates(
            shared, cr.iter_recurrence_rows(shared))
        assert len(kept) == 6 and gate.rows_excluded == 0

    def test_iter_records_the_directory_alongside_the_self_report(self, tmp_path):
        shared = tmp_path / "shared"
        _network(shared)
        self._cross(shared, dir_bot="bot-b", claims="bot-a")
        row = next(iter(cr.iter_recurrence_rows(shared)))
        assert (row.source_bot, row.bot_id) == ("bot-b", "bot-a")

    def test_an_explicit_bot_id_JOINS_the_rows_own_keys(self, tmp_path):
        """The caller's key does not REPLACE the row's — the review's R2.

        Replacing left the self-report fail-open one scope smaller: the
        scanner passes `bot_id=X` and reads `annotations/X/`, so a record
        there claiming Y was gated only as X and Y's switch never applied.
        Every call site already pairs the override with the identical
        `iter_recurrence_rows(bot_id=…)` filter, so joining costs nothing.
        """
        shared = tmp_path / "shared"
        _network(shared, **{"bot-b": False})
        self._cross(shared, dir_bot="bot-b", claims="bot-a")
        kept, gate = cr.apply_do_not_track_gates(
            shared, cr.iter_recurrence_rows(shared, bot_id="bot-b"), bot_id="bot-a")
        # bot-a (the override) is enabled, but bot-b — the directory these
        # rows physically came from — is not. Any key withholding wins.
        assert kept == []
        assert gate.exclusions[0] == cr.GateExclusion(
            bot_id="bot-b", reason=cr.GATE_SIGNAL_DISABLED, rows_excluded=6)

    def test_the_override_is_itself_a_gate_key_not_just_a_filter(self, tmp_path):
        shared = tmp_path / "shared"
        _network(shared, **{"bot-a": False})
        self._cross(shared, dir_bot="bot-b", claims="bot-b")
        kept, gate = cr.apply_do_not_track_gates(
            shared, cr.iter_recurrence_rows(shared), bot_id="bot-a")
        assert kept == []
        assert gate.exclusions[0].bot_id == "bot-a"

    def test_ANY_bots_overlay_can_exclude_the_requester_not_just_the_first(
        self, tmp_path
    ):
        """R1 — the gap the reviewer proved survived its own mutation.

        `TestTheGateKeyIsFailClosedOverEveryCandidateBot` exercised
        any-key-wins only through the per-bot SWITCH, never through the
        roster OVERLAY, so weakening the requester loop to `keys[:1]` left
        all 120 tests green. Rosters are per-bot: the overlay listing this
        requester need not belong to the directory they were read from.

        Same "asserted but never executed" class the rest of this PR is
        careful about — an invariant with two limbs needs both exercised.
        """
        shared = tmp_path / "shared"
        _network(shared)                       # both bots enabled
        self._cross(shared, dir_bot="bot-b", claims="bot-a")
        _roster(shared, "bot-a", do_not_track=["telegram:99"])   # NOT bot-b's
        kept, gate = cr.apply_do_not_track_gates(
            shared, cr.iter_recurrence_rows(shared))
        assert kept == []
        assert gate.exclusions[0] == cr.GateExclusion(
            bot_id="bot-a", reason=cr.GATE_EXCLUDED_REQUESTER, rows_excluded=6)
