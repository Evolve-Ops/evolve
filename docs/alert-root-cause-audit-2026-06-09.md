# Alert root cause: `audit_proposal` "60 proposals today" 2026-06-09

**Signal:** daily `audit` producer firing `audit_proposal` repeatedly with title
"proposals/pending: 60 proposals today exceeds limit 20 — detector may be misfiring".

The detector's own title flagged itself as suspect. It was right.

## Evidence on the mini (2026-06-09)

- Total files in `/Users/Shared/evolve/proposals/pending/`: **140**.
- Files whose `created_at` ISO date == today: **80** (genuinely new today).
- Files whose mtime > today's local midnight but `created_at` is older: **60**
  (state-history append on a days-old proposal bumps mtime).
- Detector reported **60**.

So the detector was firing on the union of the dead "filename starts with
`prop-YYYY-MM-DD`" path (always 0 hits — filenames are UUIDs) and the
mtime-based path (60 days-old proposals whose state-history was appended
today). The 80 actually-new-today proposals were invisible to it.

## Root cause

[packages/analyzer/audit.py:1786](packages/analyzer/audit.py:1786) (pre-fix):

```python
today_count = sum(
    1 for f in stage_dir.glob("*.json")
    if f.stem.startswith(f"prop-{today_prefix}") or f.stat().st_mtime > _today_start_epoch()
)
```

Two bugs in one line:

1. **Filename heuristic is dead code.** Proposal filenames are UUIDs
   (`00e44fba-41b6-48d5-8281-693d1f4ef523.json`), not `prop-2026-06-09-…`.
   The original author wrote `prop-` filenames in mind that the format never
   landed (`arbiter.store.write_proposal` uses `proposal.id`, which is a uuid4).
2. **mtime is not creation time.** `arbiter.store.write_proposal` rewrites
   the same file on every state transition (state-history append, status
   move, retry record). A proposal created 5 days ago that just moved
   `pending → snoozed → firing` today has mtime ≈ now. The detector treats
   it as "new today" and the threshold (20/day default) trips trivially.

The "detector may be misfiring" hedge text in the message confirms the
original author was unsure about the count — left in place for ~a year
because nobody fixed it.

## Fix

[packages/analyzer/audit.py:1775](packages/analyzer/audit.py:1775): count by
`created_at`.

```python
today_iso = datetime.now(timezone.utc).date().isoformat()
limit = config.get("thresholds", {}).get("maxProposalsPerDay", 100)
for stage in ("pending", "approved"):
    ...
    for f in stage_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        created = data.get("created_at")
        if isinstance(created, str) and created[:10] == today_iso:
            today_count += 1
    if today_count > limit:
        ...
```

`Proposal.created_at` is a UTC ISO timestamp (`_utc_now_iso` in
`packages/analyzer/schema/proposal.py:1316`), so comparing the `[:10]` prefix
to today's UTC date is the right reference. The `created_at` field is the
authoritative birth time; it never moves after the proposal lands.

Files that fail to JSON-parse are skipped — a single bad file does not crash
the audit.

## Threshold bump

The 20/day default was set when the pod was producing single-digit proposals
per day. Steady-state is now ~80/day legitimately (verified above), so the
operator was getting an alert every day for normal output. Bumped to
**100/day** as the new default; the operator override
(`thresholds.maxProposalsPerDay`) still works for tighter pods.

If 100/day later becomes legitimate steady-state too, the default should be
revisited — but a Signal at >100 today is genuinely worth a look.

## Message hygiene

Dropped the "— detector may be misfiring" hedge. The corrected counter
doesn't need the disclaimer:

```
proposals/pending: <n> proposals today exceeds limit <m>
```

## Tests

[packages/analyzer/tests/test_audit_proposals_volume.py](packages/analyzer/tests/test_audit_proposals_volume.py):

- a file with `created_at` == today is counted;
- a file with `created_at` == yesterday whose mtime is today is **not**
  counted (the regression guard);
- a malformed JSON file is skipped, not crashed on.

## Removed

- `_today_start_epoch()` helper in `audit.py` — only caller was the broken
  mtime path.
- `date` import from `datetime` — only use was `date.today()` in the
  filename heuristic.
