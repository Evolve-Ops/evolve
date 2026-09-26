"""required_check_health.py — D-CS9 core: a required CI check whose failure
rate crosses a threshold must be explicitly quarantined (a named owner, a
recorded reason, an expiry date) or demoted from required. Never silently
tolerated.

Spec: internal/assessment-silent-controls-2026-09-14.md §4. Sibling pattern:
tools/quarantine-ratchet + ci-quarantine.txt, the same shape applied to
individual test failures instead of whole required checks — same "frozen
ceiling, only shrinks" discipline (tools/required-check-quarantine-ratchet),
same central hand-maintained list (tools/required-check-quarantine.txt).

Verdict contract (D-CS7, packages/analyzer/verdict.py): ``measure_failure_rate``
names the check and the run ids it counted in ``evidence`` — never silence.
If the Actions API could not be read, it returns ``Verdict.unknown(...)``,
never a rate that would read as "healthy" by default. This is the same
conflation PR #4253 had to unpick for the secret-history-scan freshness
watcher, one layer up: an unreadable API is not evidence the check is fine.

Two things live here, deliberately kept apart:

  measure_failure_rate()   A pure function over already-fetched run/job data
                            (or an explicit fetch error). This is a periodic,
                            operator-run measurement — like re-running the
                            full suite before editing ci-quarantine.txt — not
                            something CI re-derives on every PR. The live
                            fetch (via ``gh api``) lives in
                            ``tools/required-check-gate``'s ``measure``
                            subcommand, deliberately outside this module, so
                            the logic above is testable with fixtures and
                            has no network dependency.

  evaluate_gate()           The cheap, network-free half that DOES belong on
                            every PR: given a measured verdict, a threshold,
                            and the parsed quarantine file, decide pass /
                            needs_quarantine / expired / unknown. An expired
                            entry always fires — "quarantine is a dated
                            promise, not a parking space" — even if the rate
                            has since improved; the owner re-measures and
                            re-files rather than letting the date ride.

A third thing, ``evaluate_report_freshness()``, applies the same "a passing
condition must not be satisfiable by absence of evidence" rule one level
up, to the report as a whole rather than one check's verdict: the checked-in
measurement carries ``measured_at``, and a report older than the caller's
``--max-age-days`` is ``stale`` — blocking, exactly like an expired
quarantine row — so the gate cannot keep passing forever on a snapshot
nobody has repeated.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "analyzer"))
from verdict import Verdict  # noqa: E402

_COUNTED_CONCLUSIONS = ("success", "failure")

REQUIRED_CHECK_NAMES: tuple[str, ...] = (
    "Admin SPA ESLint (suppressions baseline)",
    "EDR library-reuse + not-shipped smoke",
    "Full admin test suite (quarantined baseline)",
    "Full analyzer test suite (quarantined baseline)",
    "Gitleaks secrets scan",
    "Linux e2e (Ubuntu bot deploy/run/admin)",
    "OC channel-snapshot parser invariant",
    "Plugin TypeScript build",
    "Public manifest cut-line guard",
    "Public-launch scrub guard",
    "Pyright type-error ratchet (frozen baseline)",
    "Silent-exception ratchet (no new except: pass)",
)
"""The job names this control measures, kept in exactly one place so
``tools/required-check-gate measure`` — run by hand or by
``.github/workflows/required-check-measure.yml`` — never repeats this list
in YAML. Includes ``Linux e2e``, which is tracked here even though it is
not (yet) a GitHub-configured required status check — see the quarantine
row's own note."""

MEASURED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class CheckRun:
    """One completed run's conclusion for a single named required check
    (a job name within a CI workflow run). Only ``success``/``failure``
    count toward the rate — ``cancelled``/``skipped``/``neutral`` are not a
    pass/fail signal about the check's own health."""

    run_id: str
    conclusion: str


def measure_failure_rate(
    check_name: str, runs: list[CheckRun] | None, *, error: str | None = None
) -> Verdict:
    """``runs`` is whatever was actually fetched for ``check_name``; ``error``
    is set instead when the fetch itself failed. The two are mutually
    exclusive by construction of the caller, not enforced here — a caller
    that fetched successfully but found nothing passes ``runs=[]``."""
    if error is not None:
        return Verdict.unknown(
            f"could not read {check_name!r}'s run history from the Actions API: {error}"
        )

    counted = [r for r in (runs or []) if r.conclusion in _COUNTED_CONCLUSIONS]
    if not counted:
        return Verdict.unknown(
            f"no completed (success/failure) runs found for {check_name!r} in the queried window"
        )

    failing_ids = [r.run_id for r in counted if r.conclusion == "failure"]
    total = len(counted)
    rate = len(failing_ids) / total
    subject = f"{check_name}@{counted[0].run_id}..{counted[-1].run_id} (n={total})"
    return Verdict.ok(
        subject,
        evidence={
            "failure_rate": rate,
            "total": total,
            "failures": len(failing_ids),
            "run_ids": [r.run_id for r in counted],
            "failing_run_ids": failing_ids,
        },
    )


class QuarantineFormatError(ValueError):
    """A row in tools/required-check-quarantine.txt is missing a required
    field. Raised, never swallowed — a malformed row is a broken promise,
    not something to skip past."""


@dataclass(frozen=True)
class QuarantineEntry:
    check: str
    owner: str
    expiry: date
    reason: str
    raw_line: str
    lineno: int | None = None


def parse_quarantine_line(line: str, *, lineno: int | None = None) -> QuarantineEntry:
    """Row shape: ``<check>\\t<owner>\\t<expiry-date>  # <reason>`` — tab-
    separated fields (check names contain spaces and parens, e.g. "Linux
    e2e (Ubuntu bot deploy/run/admin)", so a bare-space format would be
    ambiguous). A row requires all three fields plus a non-empty reason;
    missing any of them raises ``QuarantineFormatError``."""
    raw = line.rstrip("\n")
    where = f" (line {lineno})" if lineno is not None else ""

    if "#" not in raw:
        raise QuarantineFormatError(f"missing '# <reason>'{where}: {raw!r}")
    body, _, reason = raw.partition("#")
    reason = reason.strip()
    if not reason:
        raise QuarantineFormatError(f"missing reason after '#'{where}: {raw!r}")

    fields = [f.strip() for f in body.split("\t")]
    while fields and fields[-1] == "":
        fields.pop()
    if len(fields) != 3:
        raise QuarantineFormatError(
            f"expected <check>\\t<owner>\\t<expiry-date>, got {len(fields)} "
            f"tab-separated field(s){where}: {raw!r}"
        )
    check, owner, expiry_str = fields
    if not check:
        raise QuarantineFormatError(f"row requires a check name{where}: {raw!r}")
    if not owner:
        raise QuarantineFormatError(f"row requires an owner{where}: {raw!r}")
    if not expiry_str:
        raise QuarantineFormatError(f"row requires an expiry date{where}: {raw!r}")
    try:
        expiry = date.fromisoformat(expiry_str)
    except ValueError as e:
        raise QuarantineFormatError(
            f"row requires a valid ISO expiry date (YYYY-MM-DD), got {expiry_str!r}{where}"
        ) from e

    return QuarantineEntry(
        check=check, owner=owner, expiry=expiry, reason=reason, raw_line=raw, lineno=lineno
    )


def load_quarantine(path: Path) -> dict[str, QuarantineEntry]:
    """Blank lines and lines starting with ``#`` (comments/header) are
    skipped; every other line must parse or the whole load raises. A
    missing file loads as empty — no quarantine entries, not an error (the
    file always exists once this ships, but a fresh checkout without it
    should not crash the gate at a different layer than the ratchet does)."""
    entries: dict[str, QuarantineEntry] = {}
    if not path.exists():
        return entries
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = parse_quarantine_line(line, lineno=lineno)
        entries[entry.check] = entry
    return entries


GateState = Literal["pass", "needs_quarantine", "expired", "unknown"]


@dataclass(frozen=True)
class GateResult:
    check: str
    state: GateState
    message: str
    failure_rate: float | None = None

    @property
    def blocking(self) -> bool:
        return self.state in ("needs_quarantine", "expired")


def evaluate_gate(
    check_name: str,
    verdict: Verdict,
    *,
    threshold: float,
    quarantine: dict[str, QuarantineEntry],
    today: date,
) -> GateResult:
    """The per-PR gate logic. ``verdict`` is whatever ``measure_failure_rate``
    produced (or was recorded from a prior periodic measurement) —
    ``unknown`` never silently passes as "healthy", but it also never
    manufactures a block from an absence of evidence (D-CS7); it is
    reported as its own state so a reader can tell "fine" from "I didn't
    look" apart from "over threshold"."""
    if verdict.is_unknown:
        return GateResult(check_name, "unknown", verdict.reason or "no measurement", None)

    rate = verdict.evidence.get("failure_rate")
    if rate is None:
        return GateResult(
            check_name, "unknown",
            f"{check_name!r} verdict has no failure_rate in its evidence "
            "(malformed report entry) — treated as unknown, not healthy",
            None,
        )
    entry = quarantine.get(check_name)

    # Expiry is unconditional: a lapsed date fires even if the rate has
    # since improved — "quarantine is a dated promise, not a parking
    # space" (D-CS9). A check that got better gets its row DELETED by its
    # owner, the same way a fixed test's line leaves ci-quarantine.txt;
    # letting the date silently ride is the exact failure mode this guards.
    if entry is not None and entry.expiry < today:
        return GateResult(
            check_name, "expired",
            f"quarantine for {check_name!r} expired {entry.expiry.isoformat()} "
            f"(owner {entry.owner}) — this is a Firing signal, not tolerance; "
            "re-measure and re-file, or demote from required",
            rate,
        )

    if rate <= threshold:
        return GateResult(check_name, "pass", f"{rate:.1%} <= threshold {threshold:.1%}", rate)

    if entry is None:
        return GateResult(
            check_name, "needs_quarantine",
            f"{check_name!r} failure rate {rate:.1%} exceeds threshold {threshold:.1%} "
            "with no row in tools/required-check-quarantine.txt — add an owned, dated "
            "quarantine entry or demote it from required",
            rate,
        )

    return GateResult(
        check_name, "pass",
        f"{rate:.1%} > threshold {threshold:.1%}, quarantined by {entry.owner} until "
        f"{entry.expiry.isoformat()}: {entry.reason}",
        rate,
    )


class ReportFormatError(ValueError):
    """tools/required-check-health-report.json is not the dated envelope
    shape (``{measured_at, window, checks}``) — most likely the old bare
    ``{check: verdict}`` shape from before this file carried a date. Raised,
    never silently upgraded: a report with no date must not read as fresh
    by default, the same "absence of evidence" trap D-CS7 names for a
    single verdict, one level up at the report itself."""


FreshnessState = Literal["fresh", "stale"]


@dataclass(frozen=True)
class ReportFreshness:
    state: FreshnessState
    message: str

    @property
    def blocking(self) -> bool:
        return self.state == "stale"


def evaluate_report_freshness(
    report: dict, *, max_age_days: int, today: date
) -> ReportFreshness:
    """``report`` is the parsed measurement-report JSON. Raises
    ``ReportFormatError`` if it lacks the ``measured_at``/``checks``
    envelope rather than treating a missing date as fresh. A snapshot older
    than ``max_age_days`` is ``stale`` and, like an expired quarantine row,
    that is BLOCKING — the only way ``check`` can pass forever on a
    measurement nobody has repeated is if staleness never blocks."""
    if "measured_at" not in report or "checks" not in report:
        raise ReportFormatError(
            "tools/required-check-health-report.json has no "
            "{measured_at, window, checks} envelope (the old bare "
            "{check: verdict} shape). Regenerate it with: "
            "tools/required-check-gate measure --repo <owner/repo> -n 100 "
            "> tools/required-check-health-report.json"
        )
    measured_at = report["measured_at"]
    try:
        measured_date = datetime.strptime(measured_at, MEASURED_AT_FORMAT).replace(
            tzinfo=timezone.utc
        ).date()
    except ValueError as e:
        raise ReportFormatError(
            f"measured_at {measured_at!r} is not in {MEASURED_AT_FORMAT!r} format"
        ) from e

    age_days = (today - measured_date).days
    if age_days > max_age_days:
        return ReportFreshness(
            "stale",
            f"measured {measured_at} ({age_days}d ago, limit {max_age_days}d) — "
            "re-run `tools/required-check-gate measure --repo <owner/repo> -n 100 "
            "> tools/required-check-health-report.json` and commit the refresh",
        )
    return ReportFreshness("fresh", f"measured {measured_at} ({age_days}d ago)")
