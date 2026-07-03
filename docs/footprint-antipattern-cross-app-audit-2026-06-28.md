# Footprint anti-pattern — cross-app audit (2026-06-28)

Companion to the 2026-06-28 disk-output audit (`docs/footprint-disk-output-audit-2026-06-28.md`)
that motivated the #3322 declaration contract (`packages/analyzer/footprint/components.py`
+ `tools/footprint-output-lint`). That audit found the dominant leak (537 MB of
`audit_outbox/_ingested` sediment). This follow-up sweeps the **long tail**: the
LOW-severity auto-generated surfaces that each exhibit the same *unbounded /
no-reader* shape but predate the lint, so they will not self-remediate.

Each surface below is small in isolation, but every one writes to an
auto-generated surface (`{shared_dir}/**` or `~/.openclaw/workspace/evolve/**`)
with **no bounded retention** and, in several cases, **no production reader** —
exactly the shape the #3322 contract exists to forbid. Because the lint runs
changed-only by default, an unchanged legacy producer slips past it; this batch
brings each one into compliance: a bounded retention / cap **and** an
`OutputDeclaration` in `footprint.components` (which doubles as the inventory).

Uniform remediation shape: add a BOUNDED retention/cap + register the surface in
`packages/analyzer/footprint/components.py` (and, for the re-emit ones, a
diff-gate so stable records do not re-append every run).

## Surfaces

| # | Surface | Path | Writer | Anti-pattern | Fix |
|---|---------|------|--------|--------------|-----|
| 1 | turn-annotations | `{shared}/annotations/<bot>/<date>.jsonl` | TS gateway `TurnObserver` | unbounded; readers are trailing-window (≤7d) | daily-cron 90d prune (admin-side; evolve owns the files) |
| 2 | candidates-store | `candidates/synthesis_log/<date>.jsonl`, `candidates/dropped/<date>.jsonl` | `synthesizer.py` / `store.py:record_drop` | `synthesis_log` has **zero** prod readers; `dropped/` reader hard-clamps days≤30 so >30d is unreadable sediment | daily-cron 30d prune for both |
| 3 | recommendations-log | `{shared}/<bot>/recommendations/log.jsonl` | `recommendations.py:_append_log` | unbounded; prod reads only `current.json`; stable recs re-emit `updated` daily | 90d line-age-cap + diff-gate the `updated` append |
| 4 | cascade-labels | `{shared}/cascade/labels/<date>.jsonl` | `cascade/labeler.py:write_labels` | append-only; overlapping windows re-append the same session; the dedup-tuner that would consume it does not exist yet | dedup-on-write (key session_id) + daily-cron 90d prune |
| 5 | weekly-reviews | `{shared}/reviews/<date>.md` | `weekly_review.py:write_report` | unbounded; the only reader (`/api/reviews/latest`) is dead (its SPA loader was removed) | keep-newest-12 in `write_report` + delete the orphan route + registration |
| 6 | fit-review-trail | `<bot>/fit_review/trail.jsonl` | `fit_review/runner.py:_append_trail` | unbounded; zero readers (the poller reads `outbox/`, not the trail) | soft line-cap (>1000 → keep 500), mirroring `_prune_investigations` |
| 7 | app-posture log | `{shared}/app_posture/<bot>/log/<date>.md` | `app_posture_review.py:write_posture` | unbounded; canonical `app_posture/<bot>.md` is read, the per-week LOG copy is not | keep-newest-12 on the log dir in `write_posture` |

## Notes

- **External (TS) writer (surface 1).** `annotations` is minted by the
  TypeScript gateway's `TurnObserver`, not by Python; the lint cannot govern a
  non-Python write site (same situation the `audit_pipeline` component hand-waved
  in a comment). The contract is extended minimally to accept an *external*
  (`.ts`/`.js`) writer for inventory completeness — the retention it declares is
  the admin-side prune, which evolve performs because it owns the files via ACL.
- **`consumer: none` surfaces (2 synthesis_log, 3, 5, 6, 7).** Several of these
  have no programmatic reader at all. The contract permits a justified
  `consumer: none` **only** with a finite retention window — which this batch
  supplies — so they are declared honestly rather than carrying a fictitious
  reader.
- Delivered as one PR, one commit per surface.
