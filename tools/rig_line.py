"""rig_line — D-TP10: the rig's trailing-24 h throughput as ONE computed line.

WHY (`internal/decision-rig-throughput-2026-09-15.md` §2). The rig's constraint moved three
times in one week and each move was found by a person reading the whole state; computed,
the day it moves one number goes bad. READ-ONLY: `gh api` (PRs, main's `ci` runs + jobs),
`git` (PM-STATE history), `internal/dispatch/reviews/`, the dispatch heartbeat log. THE
TOKEN'S REACH IS THE REACH: a source this call cannot read renders `unknown` — never zero,
never green. A 48-hour PR is a lane defect, listed with its owner lane and ONE action.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WINDOW_H, STALE_H, CLOSE_AFTER_D = 24, 48, 14
UNKNOWN = "unknown"
CLASSES = ("chip", "PM", "app", "dependabot")
CI_WORKFLOW_NAME = "ci"  # mirrors `meta_rig_preflight.CI_WORKFLOW_NAME`
DEFAULT_LOG_DIR = "~/.claude/meta-dispatch/log"
PM_STATE_GLOB = "internal/pm/PM-STATE-*.md"
REVIEWS_DIR = "internal/dispatch/reviews"
# Blocker classes (decision §2): A login/precondition, B lane state, C merge policy,
# D coordination, E operator-in-loop. First match wins, so the order is load-bearing.
BLOCK_RULES = (("token", "E"), ("login", "A"), ("checkout", "A"), ("main is red", "A"),
               ("main ci", "A"), ("behind", "A"), ("lane entry", "B"),
               ("lane holds id", "B"), ("lane lock", "B"), ("lane-conflict", "B"),
               ("integrity", "B"), ("back-pressure", "C"), ("files", "D"))
_OPERATOR_HEAD = re.compile(r"^#+\s.*NEEDS (THE )?OPERATOR", re.I)
_ITEM = re.compile(r"^\s*(?:\d+\.|[-*])\s+(.*\S)")


def _default_runner(argv, cwd=None):  # the one seam; tests replace it and nothing else
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _gh(run, repo, path):
    """Parsed JSON from `gh api <path>`, or None — unreadable is never an empty answer."""
    r = run(["gh", "api", "repos/{owner}/{repo}/" + path], repo)
    try:
        return json.loads(r.stdout or "null") if r.returncode == 0 else None
    except ValueError:
        return None


def classify_block(text):
    """The blocker class letter (A-E) a preflight line or `blocked_by` names, else None.
    `cap`/`prepared-cap` are not blocks: a full lane is a working lane."""
    t = str(text or "").lower()
    return (None if t.startswith(("cap", "prepared-cap"))
            else next((cls for key, cls in BLOCK_RULES if key in t), None))


def pr_class(pr):
    ref = str((pr.get("head") or {}).get("ref") or "")
    if str((pr.get("user") or {}).get("login") or "").startswith("dependabot"):
        return "dependabot"
    return ("chip" if ref.startswith("claude/meta-") else
            "PM" if ref.startswith(("pm/", "lane/")) else "app")


def owner_lane(pr):
    ref, cls = str((pr.get("head") or {}).get("ref") or ""), pr_class(pr)
    if cls == "chip":
        return "chip %s" % ref[len("claude/meta-"):]
    if cls == "PM":  # `pm/opus-review-tick-…` -> "PM opus"; `lane/state` -> "PM lane"
        return "PM %s" % (ref.split("/", 1)[1] if ref.startswith("pm/") else "lane").split("-")[0]
    return "app %s" % ref if cls == "app" else cls


def review_verdict(repo_root, n):
    """The newest review file's verdict word for PR `n` (PASS/CONCERNS/FAIL), or None."""
    d = Path(repo_root) / REVIEWS_DIR
    for f in reversed(sorted(d.glob("pr-%d.md" % n)) + sorted(d.glob("pr-%d-*.md" % n))):
        m = re.search(r"^\**Verdict:?\**:?\s*\**(PASS|CONCERNS|FAIL)", f.read_text(), re.M | re.I)
        if m:
            return m.group(1).upper()
    return None


def pr_action(age_h, verdict, red, dirty, cls="app"):
    """The ONE action that moves a 48-hour PR. `red`/`dirty` may be None (unreadable).
    A PM bookkeeping PR merges on its own verdict (D-TP2), so it is never owed a review."""
    if age_h >= CLOSE_AFTER_D * 24 and (red or dirty):  # two weeks red or conflicted
        return "close"
    if verdict is None and cls != "PM":
        return "review"
    return "fix chip" if verdict == "FAIL" or red or dirty else "merge"


def _pr_red(run, repo, sha):
    """True when the newest completed run of ANY workflow on `sha` failed; None unreadable."""
    runs = _gh(run, repo, "actions/runs?head_sha=%s&per_page=50" % sha)
    if not isinstance(runs, dict):
        return None
    latest = {r.get("name"): str(r.get("conclusion") or "").lower()  # newest per workflow
              for r in sorted(runs.get("workflow_runs") or [],
                              key=lambda r: str(r.get("updated_at"))) if r.get("status") == "completed"}
    return any(c in ("failure", "timed_out") for c in latest.values())


def stale_list(run, repo_root, open_prs, now):
    out = []
    for pr in open_prs:
        created = _ts(pr.get("created_at"))
        if created is None or now - created < timedelta(hours=STALE_H):
            continue
        n, age_h = int(pr.get("number") or 0), (now - created).total_seconds() / 3600
        state = (_gh(run, repo_root, "pulls/%d" % n) or {}).get("mergeable_state")
        dirty = None if state in (None, "unknown") else state == "dirty"
        red = _pr_red(run, repo_root, (pr.get("head") or {}).get("sha") or "")
        action = pr_action(age_h, review_verdict(repo_root, n), red, dirty, pr_class(pr))
        out.append({"number": n, "age_h": round(age_h), "lane": owner_lane(pr), "action": action})
    return sorted(out, key=lambda x: -x["age_h"])


def _run_is_red(run, repo, row, quarantine):
    if str(row.get("conclusion") or "").lower() in ("success", "skipped", "neutral"):
        return False
    jobs = _gh(run, repo, "actions/runs/%s/jobs?per_page=100" % row.get("id"))
    if not isinstance(jobs, dict):
        return None
    failed = [j.get("name") or "?" for j in jobs.get("jobs") or []
              if str(j.get("conclusion") or "").lower() == "failure"]
    return any(n not in quarantine or quarantine[n].expiry < date.today() for n in failed)


def red_main_hours(run, repo, now, quarantine=None):
    """Hours in the window during which main's newest completed `ci` push run had a
    non-quarantined failed job. None when the runs or any failed run's jobs are unreadable."""
    if quarantine is None:  # D-CS9: a quarantined, unexpired job does not redden main
        import meta_rig_preflight as mrp
        quarantine = mrp._load_quarantine_entries(
            Path(repo) / "tools" / "required-check-quarantine.txt")[0]
    data = _gh(run, repo, "actions/runs?event=push&branch=main&status=completed&per_page=100")
    if not isinstance(data, dict):
        return None
    start = now - timedelta(hours=WINDOW_H)
    rows = sorted(((t, r) for r in data.get("workflow_runs") or []
                   if r.get("name") == CI_WORKFLOW_NAME
                   and (t := _ts(r.get("updated_at"))) is not None and t <= now),
                  key=lambda x: x[0])
    first = max([i for i, (t, _) in enumerate(rows) if t <= start] or [0])
    rows, total = rows[first:], 0.0
    for i, (t, row) in enumerate(rows):
        red = _run_is_red(run, repo, row, quarantine)
        if red is None:
            return None
        if red:
            end = rows[i + 1][0] if i + 1 < len(rows) else now
            total += max(0.0, (end - max(t, start)).total_seconds())
    return total / 3600


def operator_items(text):
    items, inside = set(), False
    for line in (text or "").splitlines():
        if line.startswith("#"):
            inside = bool(_OPERATOR_HEAD.match(line))
            continue
        m = _ITEM.match(line) if inside else None
        if m:
            items.add(re.sub(r"[*`_]", "", m.group(1)).strip().lower()[:60])
        elif re.match(r"^\s*NEEDS OPERATOR:", line):
            items.add(line.split(":", 1)[1].strip().lower()[:60])
    return items


def operator_asks(run, repo_root, now):
    """(raised, resolved) across every PM-STATE file: its NEEDS OPERATOR items at HEAD vs
    the committed version one window ago. None when git cannot answer."""
    since = (now - timedelta(hours=WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raised = resolved = 0
    for f in sorted(Path(repo_root).glob(PM_STATE_GLOB)):
        rel = f.relative_to(repo_root).as_posix()
        sha = run(["git", "log", "-1", "--format=%H", "--before=" + since, "--", rel], repo_root)
        if sha.returncode != 0:
            return None
        old = (run(["git", "show", "%s:%s" % (sha.stdout.strip(), rel)], repo_root).stdout
               if sha.stdout.strip() else "")  # absent a window ago: every item is raised
        was, now_items = operator_items(old), operator_items(f.read_text())
        raised += len(now_items - was)
        resolved += len(was - now_items)
    return raised, resolved


def blocked_hours(log_dir, now):
    """`{"total": h, "by_class": {letter: h}}` from the dispatch heartbeat log — one hour
    per distinct UTC hour in which a tick recorded a block — or None when unreadable."""
    d, start, hours = Path(log_dir).expanduser(), now - timedelta(hours=WINDOW_H), {}
    if not d.is_dir():
        return None
    for day in sorted({start.date(), now.date()}):
        try:
            lines = (d / ("%s.jsonl" % day)).read_text().splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            t = _ts(rec.get("run"))
            for cls in (_record_classes(str(rec.get("note") or ""))
                        if t is not None and start < t <= now else ()):
                hours.setdefault(cls, set()).add(t.strftime("%Y%m%d%H"))
    return {"total": len(set().union(*hours.values())),
            "by_class": {c: len(h) for c, h in sorted(hours.items())}}


def _record_classes(note):
    """A heartbeat note's block classes: the tick's own `block:<letters>` stamp, else the
    verdict of a pre-stamp line (`blocked: <reason>`; a rig halt reads `?`)."""
    m = re.search(r"block:([A-E?]+)", note)
    if m:
        return set(m.group(1))
    verdict = note.split("meta-tick:", 1)[-1].split(";", 1)[0].strip()
    if verdict.startswith("blocked:"):
        return {c for c in [classify_block(verdict[len("blocked:"):].strip())] if c}
    return {"?"} if verdict.startswith("stopped at rig preflight") else set()


def compute(repo_root, *, now=None, runner=None, log_dir=DEFAULT_LOG_DIR):
    run, now = runner or _default_runner, now or datetime.now(timezone.utc)
    start = now - timedelta(hours=WINDOW_H)
    out = {"now": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    open_prs = _gh(run, repo_root, "pulls?state=open&per_page=100")
    recent = _gh(run, repo_root, "pulls?state=all&sort=updated&direction=desc&per_page=100")
    if isinstance(recent, list):
        merged = [p for p in recent if (_ts(p.get("merged_at")) or start) > start]
        out["merges"] = dict(Counter(pr_class(p) for p in merged))
        out["chips_built"] = sum(1 for p in recent if pr_class(p) == "chip"
                                 and (_ts(p.get("created_at")) or start) > start)
    if isinstance(open_prs, list):
        ages = sorted((now - _ts(p["created_at"])).total_seconds() / 3600
                      for p in open_prs if _ts(p.get("created_at")))
        out["open"] = len(ages)
        out["median_age_h"] = statistics.median(ages) if ages else 0
        out["stale"] = stale_list(run, repo_root, open_prs, now)
    out["operator"] = operator_asks(run, repo_root, now)
    out["red_main_h"] = red_main_hours(run, repo_root, now)
    out["blocked"] = blocked_hours(log_dir, now)
    return out


def _h(x):
    return UNKNOWN if x is None else ("%dh" % round(x) if x >= 10 else "%.1fh" % x)


def render(d):
    m = d.get("merges")
    merges = (UNKNOWN if m is None else "%d (%s)" % (sum(m.values()), " · ".join(
        "%s %d" % (c, m.get(c, 0)) for c in CLASSES)))
    stale = d.get("stale")
    stale_s = (UNKNOWN if stale is None else "%d%s" % (len(stale), " (%s)" % ", ".join(
        "#%d" % s["number"] for s in stale) if stale else ""))
    op, b = d.get("operator"), d.get("blocked")
    blocked = (UNKNOWN if b is None else "%dh%s" % (b["total"], " (%s)" % " ".join(
        "%s%d" % kv for kv in b["by_class"].items()) if b["by_class"] else ""))
    return ("rig 24h: merges %s · chips built %s · open PRs %s, median age %s · >48h %s · "
            "operator asks +%s/−%s · main red %s · lane blocked %s"
            % (merges, d.get("chips_built", UNKNOWN), d.get("open", UNKNOWN),
               _h(d.get("median_age_h")), stale_s, *(op or (UNKNOWN, UNKNOWN)),
               _h(d.get("red_main_h")), blocked))


def report(repo_root, **kw):
    """`{line, stale_lines, data}` — the one call every consumer makes."""
    d = compute(repo_root, **kw)
    return {"line": render(d), "data": d, "stale_lines": [
        "  48h+ #%d (%dh, %s) → %s" % (s["number"], s["age_h"], s["lane"], s["action"])
        for s in d.get("stale") or []]}


def main(argv=None):  # `python3 tools/rig_line.py [--json]` from the repo root
    out = report(Path.cwd())
    print(json.dumps(out, indent=1, ensure_ascii=False) if "--json" in (argv or sys.argv[1:])
          else "\n".join([out["line"]] + out["stale_lines"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
