"""meta-tick — the PM-lane dispatch tick as ONE command (D-MT1, D-MT2).

`internal/decision-meta-tick-is-a-command-2026-09-23.md`. The hourly `meta-dispatch`
session used to read an 892-line procedure and execute it step by step; every recent lane
defect was a step it did not follow, and a tick that did nothing cost ~2-3M input tokens
(`internal/finding-tick-cost-2026-09.md`). This module runs that procedure's steps 0 -> 7
in order — each step a subprocess call to the granted verb the procedure already named —
and hands the session only what a script cannot do: prepare a tray card (`spawn_task`) and
send a poke (`PushNotification`). The procedure it implements is now its specification:
`internal/meta-tick-spec.md`.

THE RUNNER NEVER RE-DERIVES A RULE A HELPER OWNS. Eligibility, both caps, the file-set
rule, the route, liveness, the lane-PR poke signature and the card ages are read from the
helpers' `--json`; this file only sequences them and applies the procedure's STOP rules:

  * rig `may_reconcile: false`  -> print its lines, heartbeat, stop.
  * rig `may_launch: false`     -> reconcile and bookkeep, launch and prepare nothing.
  * `sync` refused              -> carry on to eligibility, but never `land` this tick.
  * stale-checkout / lane-conflict / back-pressure -> dispatch nothing.
  * a headless start only when `next.route` says so AND `rig-preflight --probe-login`
    (once, before the first start) says `may_launch`.

Every subprocess goes through `Tick.call`, which refuses an argv that is not in
`GRANTED` — the list is the claim "`meta-tick` widens no grant", and a test reads it.

Pokes are edge-triggered on the signature format the procedure has always written to
`last-seen.json` (`<class>:<kind>:<id|path>`, `lane:<…>`). The runner records a poke's
signature BEFORE the session sends it, so a session that dies mid-send never re-pokes
forever; the text is also kept in `last-seen.json`'s `unsent[]` until the session runs
`meta-tick ack`, and a tick that finds `unsent[]` still populated re-emits it ONCE.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import meta_dispatch_integrity as mdi  # noqa: E402
import rig_line  # noqa: E402  (D-TP10: the blocker-class vocabulary, not its `gh` reach)

MOVE = ("python3", "tools/meta-dispatch-move")
ELIGIBLE = ("python3", "tools/meta-dispatch-eligible")
INFLIGHT = ("python3", "tools/meta-inflight")
# Every command this runner may execute, as argv prefixes. Each is already granted to the
# `meta-dispatch` task (`internal/meta-system-setup.md`); `meta-tick` itself is the one
# new grant. Adding a prefix here is adding a grant — the test pins this tuple.
GRANTED = (MOVE, ELIGIBLE, INFLIGHT, ("gh", "pr", "list"), ("gh", "pr", "view"))

DEFAULT_REPO = "cjalden/evolve"
LANE_DIR = "internal/dispatch"
LAST_SEEN = "~/.claude/meta-dispatch/last-seen.json"
POKE_BUDGET = 200        # step 6: one line, <=200 chars
TITLE_CLIP = 64          # step 4d: the title is the only part that yields
CARD_REPEAT_H = (12, 6)  # D-CD4, highest first
FAILED_PREP_H = 24       # step 1: no pr and no session for ~24h
TIMEOUT_S = 900
_STOP = {"the", "that", "this", "with", "from", "into", "when", "every", "never", "chip",
         "brief", "does", "what", "which", "their", "there", "than", "then", "only",
         "have", "must", "each", "part", "plus", "fix", "hold"}


class NotGranted(RuntimeError):
    pass


def granted(argv) -> bool:
    return any(tuple(argv[:len(g)]) == g for g in GRANTED)


def clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def keywords(title: str, n: int = 4) -> str:
    """3-5 comma-separated words from the title (step 3). Comma form, always: an older
    `meta-inflight` matched a bare phrase as one token and answered `0`."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", title):
        w = w.lower()
        if w not in _STOP and w not in out:
            out.append(w)
        if len(out) == n:
            break
    return ",".join(out)


def fm_value(text: str, key: str):
    """One flat front-matter scalar (quoted or bare), or None for absent/null/~."""
    try:
        fm = mdi.split_front_matter(text).fm_lines
    except mdi.FrontMatterError:
        return None
    pat = re.compile(r"^%s\s*:\s*(.*?)\s*$" % re.escape(key))
    for line in fm:
        m = pat.match(line)
        if not m:
            continue
        raw = m.group(1)
        if raw.startswith('"'):
            try:
                return json.loads(raw)
            except ValueError:
                return raw.strip('"')
        raw = raw.strip("'")
        if raw in ("", "null", "~"):
            return None
        return {"true": True, "false": False}.get(raw.lower(), raw)
    return None


def _run(argv, cwd):
    return subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True,
                          timeout=TIMEOUT_S)


class Call:
    def __init__(self, argv, code, out, err):
        self.argv, self.code, self.out, self.err = list(argv), code, out or "", err or ""
        try:
            self.data = json.loads(self.out) if self.out.strip() else None
        except ValueError:
            self.data = None

    @property
    def ok(self):
        return self.code == 0

    def why(self):
        return clip((self.err or self.out).strip().splitlines()[-1]
                    if (self.err or self.out).strip() else "exit %d" % self.code, 240)


class Tick:
    def __init__(self, repo_root=".", *, run=None, last_seen_path=LAST_SEEN,
                 dry_run=False):
        self.root = Path(repo_root)
        self.lane = self.root / LANE_DIR
        self._run = run or _run
        self.last_seen_path = Path(last_seen_path).expanduser()
        self.dry_run = dry_run
        self.argvs = []          # every argv executed, in order (the tests read this)
        self.lines = []          # what happened, one line per fact
        self.actions = []        # session_actions[]
        self.active = set()      # signatures whose condition holds this tick
        self.poked_now = []      # new signatures recorded this tick
        self.unsent = []         # this tick's fresh pokes, kept until `ack`
        self.full = False        # reached the eligibility step (prune only then)
        self.land_ran = False
        self.block = set()       # D-TP10 blocker classes (A-E, `?`) this tick hit
        self.rig_line = None     # D-TP10 line, recorded on the heartbeat when computed
        self.last = self._last_seen()
        self.repo = self._repo_slug()

    # ── plumbing ────────────────────────────────────────────────────────────────
    def call(self, *argv) -> Call:
        if not granted(argv):
            raise NotGranted("meta-tick may only run granted verbs, not %r" % (argv,))
        self.argvs.append(list(argv))
        try:
            r = self._run(argv, self.root)
        except (OSError, subprocess.SubprocessError) as e:
            return Call(argv, 127, "", str(e))
        return Call(argv, r.returncode, r.stdout, r.stderr)

    def move(self, *args):
        return self.call(*MOVE, *args)

    def _last_seen(self) -> dict:
        try:
            d = json.loads(self.last_seen_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _repo_slug(self) -> str:
        try:
            slug = json.loads((self.root / ".claude" / "meta.json")
                              .read_text(encoding="utf-8")).get("repo_slug")
        except (OSError, ValueError, AttributeError):
            slug = None
        return slug if isinstance(slug, str) and "/" in slug else DEFAULT_REPO

    def poke(self, signature: str, text: str) -> None:
        """Edge-triggered (step 6): one poke per signature, ever, until it is pruned."""
        self.active.add(signature)
        if signature in (self.last.get("poked") or []) or signature in self.poked_now:
            return
        item = {"kind": "poke", "text": clip(text, POKE_BUDGET), "signature": signature}
        self.poked_now.append(signature)
        self.unsent.append(item)
        self.actions.append(item)

    def entry(self, state: str, brief_id: str):
        p = self.lane / state / ("%s.md" % brief_id)
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    # ── the tick ────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        start = (self.move("now", "--json").data or {}).get("now")
        for item in self.last.get("unsent") or []:
            if isinstance(item, dict) and item.get("text"):
                self.actions.append(dict(item, kind="poke", retry=True))
                self.lines.append("re-sending 1 unacknowledged poke: %s"
                                  % item.get("signature"))
        rig = self.move("rig-preflight", "--json")
        rd = rig.data or {}
        if not rd:
            rd = {"may_reconcile": False, "may_launch": False, "ok": False,
                  "lines": ["rig: rig-preflight did not answer — %s" % rig.why()],
                  "login_note": "login: not probed this tick"}
        self.lines.extend(rd.get("lines") or ["rig: all clear"])
        self.lines.append(rd.get("login_note") or "")
        self.block = {rig_line.classify_block(ln) or "?" for ln in rd.get("lines") or []}
        # D-TP10: the rig line prints right after the preflight lines, halted tick or not —
        # a stopped lane is exactly the day the line has to be read.
        rl = self.move("rig-line", "--json")
        self.rig_line = (rl.data or {}).get("line")
        self.lines.append(self.rig_line or "rig line: unavailable — %s" % rl.why())
        self.lines.extend((rl.data or {}).get("stale_lines") or [])
        if not rd.get("may_reconcile"):
            kinds = sorted({(ln.split(":", 1)[-1].split() or ["?"])[0].strip("'")
                            for ln in rd.get("lines") or []})
            self.poke("lane:rig-halt:%s" % "+".join(kinds),
                      "Lane stopped at rig preflight — %s"
                      % ((rd.get("lines") or ["rig red"])[0]))
            return self.finish(start, verdict="stopped at rig preflight (may_reconcile: "
                                               "false)")
        may_launch = bool(rd.get("may_launch"))

        dry = ["--dry-run"] if self.dry_run else []
        sync = (self.move("sync", *dry, "--json").data
                or {"pulled": False, "reason": "no-answer", "message": ""})
        synced = bool(sync.get("pulled")) or self.dry_run
        self.lines.append("sync: %s%s" % (sync.get("reason"),
                                          " — %s" % sync["message"]
                                          if sync.get("message") else ""))
        rep = self.move("repair", *dry, "--json")
        self.repair_line = (rep.data or {}).get("line") or "repair: %s" % rep.why()

        merged, reconciled = self.reconcile()
        self.recover_stalls()
        el_args = ["--json"] + (["--merged-prs", ",".join(map(str, merged))]
                                if merged else [])
        el = self.call(*ELIGIBLE, *el_args)
        e = el.data
        if not isinstance(e, dict):
            self.lines.append("eligibility did not answer — %s; nothing dispatched"
                              % el.why())
            return self.finish(start, verdict="eligibility helper failed",
                               reconciled=reconciled, synced=synced)
        self.full = True
        self.elig = e
        dispatch_ok = self.read_eligibility(e, sync)
        started = cards = 0
        gated = []
        if not may_launch:
            self.lines.append("rig may_launch: false — nothing duplicate-checked or "
                              "launched this tick")
        elif dispatch_ok:
            started, cards, gated = self.launch_candidates(e, el_args)
        self.render_cards(e, moved=started)
        return self.finish(start, verdict=None, reconciled=reconciled, started=started,
                           cards=cards, gated=gated, synced=synced)

    def render_cards(self, e, *, moved):
        """D-CD3's `CARDS.md`, rendered at the END of the tick (after every move), so a
        card this tick started is in the file the tick lands. When nothing moved, step
        2's `cards[]` still describes the lane and is handed over as-is — no second
        scan; when something did, `cards` re-reads `inflight/` (without `evaluate`).
        A prepared card lands in the file via `meta-tick card`, which moves it."""
        if self.dry_run:
            return
        if moved:
            self.move("cards", "--json")
        else:
            self.move("cards", "--cards-json",
                      json.dumps(e.get("cards") or [], ensure_ascii=False), "--json")

    def reconcile(self):
        """Step 1: each `inflight/` entry against gh. Returns (merged PRs, reconciled)."""
        entries = {}
        for p in sorted((self.lane / "inflight").glob("*.md")):
            text = p.read_text(encoding="utf-8", errors="replace")
            entries[p.stem] = {k: fm_value(text, k) for k in
                               ("pr", "session", "dispatched_at", "aspect", "title")}
        page = self.call("gh", "pr", "list", "--repo", self.repo, "--state", "all",
                         "--limit", "80", "--json", "number,headRefName,state,mergedAt")
        prs = {p["number"]: p for p in (page.data or []) if isinstance(p, dict)}
        merged = sorted(n for n, p in prs.items() if p.get("state") == "MERGED")
        reconciled, unbound = 0, []
        for bid, fm in entries.items():
            pr = mdi.parse_pr_ref(fm["pr"]) if fm["pr"] is not None else None
            if pr is None:
                if fm["session"]:
                    unbound.append(bid)
                else:
                    age = mdi.age_hours(fm["dispatched_at"])
                    if age is not None and age >= FAILED_PREP_H:
                        self.poke("1:no-pr-session:%s" % bid,
                                  "[%s] no pr/session %dh after dispatch — failed "
                                  "preparation, left in place" % (bid, age))
                continue
            state = (prs.get(pr) or {}).get("state")
            if state is None:
                v = self.call("gh", "pr", "view", str(pr), "--repo", self.repo,
                              "--json", "state,mergedAt,headRefName")
                state = (v.data or {}).get("state")
            if state == "MERGED":
                reconciled += self.terminal(bid, "done", pr, "merged")
            elif state == "CLOSED":
                reconciled += self.terminal(bid, "abandon", pr, "closed_superseded")
        if unbound:
            self.bind_unbound({e.get("pr") for e in entries.values()})
        for bid in entries:
            if (self.lane / "queued" / ("%s.md" % bid)).is_file() and not self.dry_run:
                r = self.move("repair-queued-copy", bid, "--json")
                if r.ok and (r.data or {}).get("log"):
                    self.lines.append(r.data["log"])
        return merged, reconciled

    def recover_stalls(self) -> None:
        """Step 1b: `meta-dispatch-move recover-stalls` — the D-TP1 step-4 sweep that
        nothing was calling (found 2026-09-24: `mail-ingest-strips-login-secrets` died
        before its first push at 17:02 PDT 09-23, sat 23 h, and the eligibility helper
        read the launcher as STALLED — `headless.available: false` for every brief —
        while the verb that abandons a four-way-corroborated dead entry and requeues its
        successor existed and ran nowhere: not here, not in the reconcile procedure).

        Why it belongs in THIS tick and does not double-mint with the reconciler: the
        reconciler relaunches only a chip that STARTED (has a branch or a pushed commit);
        `recover-stalls` acts only on a headless entry with NO branch, no ref, no open
        PR and no worktree write past the grace window — disjoint sets — and it is
        idempotent on its own successor (`find_existing_successor`). Anything ONE source
        contradicts is reported as UNKNOWN and left alone; the verb decides, this runner
        only calls it and prints what it said. Skipped on a dry run (it moves entries).
        """
        if self.dry_run:
            self.lines.append("would sweep stalled headless entries (recover-stalls)")
            return
        r = self.move("recover-stalls", "--json")
        d = r.data if isinstance(r.data, dict) else None
        if d is None:
            self.lines.append("recover-stalls did not answer — %s" % r.why())
            return
        for ln in d.get("lines") or []:
            if ln and ln != "recover-stalls: nothing past the grace window to check":
                self.lines.append(ln)
        for row in d.get("recovered") or []:
            self.poke("1:stall-recovered:%s" % row.get("id"),
                      "[%s] headless session died before its first push — abandoned, "
                      "successor %s queued" % (row.get("id"), row.get("successor") or "not queued"))

    def terminal(self, bid, verb, pr, bucket) -> int:
        if self.dry_run:
            self.lines.append("would %s %s (PR #%d)" % (verb, bid, pr))
            return 0
        args = [verb, bid] + (["--pr", str(pr)] if verb == "abandon" else []) + ["--json"]
        r = self.move(*args)
        if not r.ok:
            msg = r.why()
            self.lines.append("%s %s refused: %s" % (verb, bid, msg))
            if verb == "abandon" or "does not contain" in msg or "integrity" in msg:
                self.poke("3:integrity:%s" % bid, "[%s] %s refused on PR #%d — %s"
                          % (bid, verb, pr, msg))
            return 0
        self.lines.append("%s: %s (PR #%d %s)" % (verb, bid, pr, bucket))
        rr = self.move("chip-row", bid, "--set", json.dumps({"bucket": bucket, "pr": pr}),
                       "--json")
        if not rr.ok:
            self.lines.append("chip row for %s not updated: %s" % (bid, rr.why()))
        return 1

    def bind_unbound(self, bound_prs) -> None:
        """Step 1's backstop: an open PR whose diff moves a lane entry binds itself."""
        page = self.call("gh", "pr", "list", "--repo", self.repo, "--state", "open",
                         "--limit", "80", "--json", "number,headRefName,files")
        for p in page.data or []:
            files = [f.get("path", "") for f in p.get("files") or []]
            if (str(p.get("number")) in {str(b) for b in bound_prs if b is not None}
                    or not any(re.match(r"internal/dispatch/(inflight|done)/", f)
                               for f in files)):
                continue
            if self.dry_run:
                self.lines.append("would bind PR #%s" % p["number"])
                continue
            r = self.move("bind", "--pr", str(p["number"]), "--files",
                          json.dumps({"files": p["files"]}), "--json")
            if r.code == 0:
                self.lines.append("bound: PR #%s -> %s" % (p["number"],
                                                           (r.data or {}).get("id")))
            elif r.code == 3:
                d = r.data or {}
                cands = d.get("candidates") or [{}]
                self.poke("3:orphan-id:%s" % p["number"],
                          "PR #%s closes out under unknown id %s — most likely %s"
                          % (p["number"], d.get("id"), cands[0].get("id")))

    def read_eligibility(self, e, sync) -> bool:
        """Step 2's stops and pokes, read straight off the helper. True = may dispatch."""
        ok = True
        blocked = e.get("blocked_by")
        base = e.get("base") or {}
        if blocked == "stale-checkout":
            self.poke("lane:stale-checkout", "Lane dispatches nothing: checkout %s behind "
                      "%s — sync: %s" % (base.get("behind"), base.get("ref"),
                                         sync.get("message") or sync.get("reason")))
            ok = False
        for c in e.get("conflicts") or []:
            self.poke("3:conflict:%s" % c["id"], "[%s] is in %s — lane dispatches nothing "
                      "until the stale copy is deleted" % (c["id"],
                                                          "+".join(c.get("states", []))))
            ok = False
        for rp in e.get("repairable") or []:
            argv = shlex.split(rp.get("command", "")) + ["--json"]
            if self.dry_run or not granted(argv):
                self.lines.append("would repair: %s" % rp.get("command"))
                continue
            r = self.call(*argv)
            self.lines.append((r.data or {}).get("log") or "repair %s: %s"
                              % (rp.get("id"), r.why()))
        for o in e.get("orphan_done") or []:
            self.poke("3:orphan-done:%s" % o["id"], "[%s] done/ holds the brief in flight "
                      "as [%s] — one chip under two ids; a human picks the id"
                      % (o["id"], o.get("inflight_id")))
        for bad in e.get("invalid") or []:
            self.poke("3:invalid:%s" % bad["path"], "Malformed brief %s — %s"
                      % (bad["path"], bad.get("error")))
        if (e.get("back_pressure") or {}).get("paused"):
            self.poke("2:back-pressure", "PM: %d lane PRs await review (%s) — dispatch "
                      "paused" % (len(e["back_pressure"].get("unreviewed_prs", [])),
                                  ", ".join("#%s" % n for n in
                                            e["back_pressure"].get("unreviewed_prs", []))))
            ok = False
        for h in e.get("held") or []:
            if h.get("reason") == "privileged-without-greenlight":
                self.poke("2:greenlight:%s" % h["id"], "[%s] is privileged and waits on "
                          "your greenlight" % h["id"])
        for live in e.get("headless_liveness") or []:
            if live.get("state") in ("stalled", "unknown"):
                self.poke("1:headless-%s:%s" % (live["state"], live["id"]),
                          "[%s] headless session %s %s — output: gh pr list --head "
                          "claude/meta-%s" % (live["id"], live.get("session"),
                                              live["state"], live["id"]))
        for c in e.get("cards") or []:
            # Only an UNTAPPED card repeats: `prepared`, or `stale` whose `stale_kind` is
            # `prepared`. A stale STARTED card has a session on it.
            if not (c.get("state") == "prepared"
                    or (c.get("state") == "stale" and c.get("stale_kind") == "prepared")):
                continue
            self.active.add("1:card:%s:0h" % c["id"])
            age = c.get("age_h") or 0
            for i, h in enumerate(CARD_REPEAT_H):
                if age >= h:
                    title = (c.get("tray_title") or "").split("[%s] " % c["id"], 1)[-1]
                    self.poke("1:card:%s:%dh" % (c["id"], h), "[META:%s] [%s] %s "
                              "prepared %dh — tap or decline"
                              % (c.get("aspect"), c["id"], clip(title, TITLE_CLIP), h))
                    for lower in CARD_REPEAT_H[i + 1:]:
                        self.active.add("1:card:%s:%dh" % (c["id"], lower))
                    break
        if blocked in ("lane-conflict", "back-pressure", "stale-checkout"):
            ok = False
        return ok

    def launch_candidates(self, e, el_args):
        """Steps 3-5: duplicate-check each candidate, then launch by `next.route`."""
        unmoved = {u.get("id") for u in self.last.get("prepared_but_unmoved") or []
                   if isinstance(u, dict)}
        survivors, gated = [], []
        for c in e.get("candidates") or []:
            if c["id"] in unmoved:
                self.lines.append("skip %s: in prepared_but_unmoved[]" % c["id"])
                continue
            args = [*INFLIGHT, "--aspect", c["aspect"], "--candidate", c["id"],
                    "--keywords", keywords(c["title"]), "--repo", self.repo, "--json"]
            if c.get("repair_pr"):
                # A repair brief overlaps the PR it repairs by definition; without this
                # the duplicate check read #4440 as "someone else is on the work" and
                # gated `hold-fix-4440` every tick for two days (2026-09-25 23:01 PDT:
                # `gated … actionable_count 1 (top: <#4440's own title>)`).
                args += ["--repairs", str(c["repair_pr"])]
            r = self.call(*args)
            d = r.data if isinstance(r.data, dict) else None
            if d is None or d.get("warnings"):
                gated.append(c["id"])
                self.lines.append("gated %s: duplicate check could not answer (%s)"
                                  % (c["id"], (d or {}).get("warnings") or r.why()))
            elif d.get("actionable_count", 0) > 0:
                gated.append(c["id"])
                top = (d.get("overlaps") or [{}])[0]
                self.lines.append("gated %s: actionable_count %d (top: %s)"
                                  % (c["id"], d["actionable_count"],
                                     clip(top.get("title", ""), 80)))
            else:
                survivors.append(c)
        if gated and not survivors:
            self.lines.append("duplicates_exhausted")
        started = cards = 0
        slots = e.get("headless_slots", 0) or 0
        probed, may_start = False, True
        for c in survivors:
            route = c.get("route")
            if route == "headless":
                if slots <= 0:
                    self.lines.append("hold %s: no headless slot left this tick" % c["id"])
                    continue
                if not probed and self.dry_run:
                    probed = True     # the probe spawns a session: never on a dry run
                    self.lines.append("would probe login before the first headless start")
                elif not probed:
                    probed = True
                    pd = self.move("rig-preflight", "--probe-login", "--json").data or {}
                    may_start = bool(pd.get("may_launch"))
                    self.lines.append(pd.get("login_note") or "login: probe did not answer")
                    if not may_start:
                        self.lines.extend(pd.get("lines") or [])
                if may_start:
                    res = self.start_headless(c)
                    if res == "stop":
                        break
                    if res == "started":
                        started += 1
                        again = self.call(*ELIGIBLE, *el_args, "--headless-started",
                                          str(started))
                        slots = ((again.data or {}).get("headless_slots", 0)
                                 if again.ok else 0)
                        continue
                    if res == "skip":
                        continue
            if self.dry_run:
                self.lines.append("would prepare card: %s" % c["id"])
                cards += 1
                continue
            self.prepare(c)
            cards += 1
        return started, cards, gated

    def start_headless(self, c) -> str:
        if self.dry_run:
            self.lines.append("would start headless: %s" % c["id"])
            return "skip"
        r = self.move("launch", c["id"], "--headless", "--json")
        d = r.data or {}
        if r.ok and d.get("moved"):
            self.lines.append("started headless: %s session %s in %s"
                              % (c["id"], d.get("session"), d.get("worktree")))
            self.row(c, d, launch="headless", task_id=d.get("session"))
            return "started"
        if r.ok:
            self.lines.append("%s: %s — preparing a card instead" % (c["id"], d.get("note")))
            return "card"
        msg = r.why()
        self.lines.append("launch %s refused: %s" % (c["id"], msg))
        if "RUNNING" in msg:
            m = re.search(r"session (\S+) is already RUNNING", msg)
            self.new_unmoved = [{"id": c["id"], "task_id": m.group(1) if m else None}]
            self.poke("1:launch-failed:%s" % c["id"], "[%s] headless session running but "
                      "the entry did not move — recorded in prepared_but_unmoved"
                      % c["id"])
            return "stop"
        if msg.startswith("meta-dispatch-move: refused: integrity"):
            self.poke("3:integrity:%s" % c["id"], "[%s] integrity refusal — %s"
                      % (c["id"], msg))
        return "skip"

    def prepare(self, c) -> None:
        text = self.entry("queued", c["id"]) or ""
        try:
            body = mdi.split_front_matter(text).body
        except mdi.FrontMatterError:
            self.poke("3:invalid:%s" % c.get("path"), "Brief %s has no front matter"
                      % c.get("path"))
            return
        self.actions.append({
            "kind": "prepare_card", "id": c["id"], "aspect": c["aspect"],
            "title_timed": c.get("chip_title_timed") or c["chip_title"],
            "body_path": c.get("path"), "body": body,
            "privileged": bool(c.get("privileged")),
            "then": "python3 tools/meta-tick card %s --task-id <task_id>" % c["id"]})
        self.lines.append("card to prepare: %s" % c["id"])

    def row(self, c, d, *, launch, task_id) -> None:
        text = self.entry("inflight", c["id"]) or ""
        self.move("chip-row", c["id"], "--aspect", c["aspect"], "--append", json.dumps({
            "title": c["title"], "task_id": task_id, "pr": None, "branch": None,
            "bucket": "dispatched", "two_pass": None, "privileged": bool(c["privileged"]),
            "reversible": fm_value(text, "reversible"), "dispatched": d.get("dispatched"),
            "dispatched_at": d.get("dispatched_at"), "why": d.get("why"),
            "launch": launch, "note": "PM lane: %s/inflight/%s.md" % (LANE_DIR, c["id"])},
            ensure_ascii=False), "--json")

    # ── step 6b, 7: land, heartbeat, state, report ──────────────────────────────
    def finish(self, start, *, verdict, reconciled=0, started=0, cards=0, gated=(),
               synced=False) -> dict:
        e = getattr(self, "elig", None) or {}
        lane_pr_line = None
        if self.full and not synced:
            self.lines.append("land: skipped — sync refused, so this tick never lands")
        elif self.full:
            self.land_ran = True
            r = self.move("land", *(["--dry-run"] if self.dry_run else []), "--json")
            d = r.data or {}
            if r.ok:
                lane_pr_line = d.get("lane_pr_line")
                if d.get("landed"):
                    self.lines.append("landed: %s" % d.get("pr_url"))
                sig = (d.get("lane_prs") or {}).get("poke_signature")
                if sig:
                    self.poke("lane:%s" % sig, "Lane: %s — the standing lane/state PR is "
                              "not merging" % lane_pr_line)
            else:
                msg = r.why()
                kind = ("behind" if "moved on origin" in msg else "lease" if "stale info"
                        in msg else "foreign-commit" if "Lane-State" in msg else
                        "integrity" if "integrity" in msg else "other")
                self.poke("lane:land-refused:%s" % kind, "Lane did not land (%s): %s"
                          % (kind, msg))
        if verdict is None:
            nxt = []
            if started:
                nxt.append("%d headless start(s)" % started)
            if cards:
                nxt.append("%d card(s) to prepare" % cards)
            verdict = ", ".join(nxt) or ("blocked: %s" % e["blocked_by"]
                                         if e.get("blocked_by") else "quiet")
        report = self.report(verdict, e, lane_pr_line, start)
        self.block |= {c for c in [rig_line.classify_block(e.get("blocked_by"))] if c}
        hb = None
        if not self.dry_run:
            args = ["heartbeat", "--prepared", str(started + cards), "--reconciled",
                    str(reconciled), "--held", str(len(gated)), "--invalid",
                    str(len(e.get("invalid") or []))]
            if (e.get("back_pressure") or {}).get("paused"):
                args.append("--paused")
            if start:
                args += ["--started", start]
            if self.rig_line:
                args += ["--rig-line", self.rig_line]
            args += ["--note", clip("meta-tick: %s; %spokes %d%s" % (
                verdict, "block:%s; " % "".join(sorted(self.block)) if self.block else "",
                len(self.unsent),
                "; unsent %d" % len(self.unsent) if self.unsent else ""), 400), "--json"]
            hb = self.move(*args).data
            self.write_state(e.get("now") or start)
        return {"report": report, "heartbeat": hb, "session_actions": self.actions,
                "lines": self.lines, "dry_run": self.dry_run}

    def write_state(self, now) -> None:
        state = dict(self.last)
        old = [s for s in state.get("poked") or [] if isinstance(s, str)]
        keep = []
        for s in old:
            evaluated = self.full and (not s.startswith("lane:lane-state") or self.land_ran)
            if not evaluated or s in self.active:
                keep.append(s)
        state["poked"] = keep + [s for s in self.poked_now if s not in keep]
        state["unsent"] = self.unsent
        state["prepared_but_unmoved"] = ((state.get("prepared_but_unmoved") or [])
                                         + getattr(self, "new_unmoved", []))
        if now:
            state["last_run"] = now
        self.move("state", "--lane", "dispatch", "--json",
                  json.dumps(state, ensure_ascii=False))

    def report(self, verdict, e, lane_pr_line, start=None) -> str:
        L = ["meta-dispatch %s — %s" % (e.get("now") or start or "", verdict)]
        L += [ln for ln in self.lines if ln]
        if e:
            L.append(summary_line(e))
            L.append(e.get("cards_waiting_line") or "cards waiting for a tap: none")
        L.append(getattr(self, "repair_line", None) or "repair: not run this tick")
        if lane_pr_line:
            L.append(lane_pr_line)
        return "\n".join(L)


def summary_line(e) -> str:
    """The eligibility helper's own summary line (`render_text`), verbatim."""
    s = ("in flight: %d/%d · prepared: %d/%d · slots: %d in-motion, %d prepared"
         % (e.get("in_motion_slots", e.get("in_motion_count", 0)), e.get("cap", 0),
            e.get("prepared_count", 0), e.get("prepared_cap", 0), e.get("slots", 0),
            e.get("prepared_slots", 0)))
    if e.get("held_prs"):
        s += " · %d held: %s" % (len(e["held_prs"]),
                                 ", ".join("#%d" % p for p in e["held_prs"]))
    hl = e.get("headless") or {}
    if hl:
        s += " · headless: %s" % ("%d/%d this tick" % (e.get("headless_slots", 0),
                                                       hl.get("per_tick", 0))
                                  if hl.get("available") else "unavailable")
    return s + (" · %s" % e["base_line"] if e.get("base_line") else "")


def card(tick: Tick, brief_id: str, task_id: str) -> dict:
    """After the session's `spawn_task`: the move, the chip row, the poke (step 4b-4d).

    Spawn THEN move is the procedure's order (HARD RULES: the move only after the spawn
    returned an id), which is why this is a second command and not part of `run`."""
    queued = tick.entry("queued", brief_id) or ""
    aspect, title = fm_value(queued, "aspect"), fm_value(queued, "title") or brief_id
    r = tick.move("launch", brief_id, "--session", task_id, "--json")
    if not r.ok:
        tick.move("state", "--lane", "dispatch", "--merge", json.dumps(
            {"prepared_but_unmoved": [{"id": brief_id, "task_id": task_id}],
             "poked": ["1:launch-failed:%s" % brief_id]}))
        return {"ok": False, "error": r.why(), "poke": {
            "text": clip("[META:%s] [%s] launch failed: card %s is in the tray but the "
                         "entry did not move — %s" % (aspect, brief_id, task_id, r.why()),
                         POKE_BUDGET), "signature": "1:launch-failed:%s" % brief_id}}
    d = r.data or {}
    c = {"id": brief_id, "aspect": aspect, "title": title,
         "privileged": fm_value(queued, "privileged") is not False}
    tick.row(c, d, launch="prepared", task_id=task_id)
    tick.move("cards", "--json")   # the card now exists in inflight/: put it in CARDS.md
    sig = "1:card:%s:0h" % brief_id
    tick.move("state", "--lane", "dispatch", "--merge", json.dumps({"poked": [sig]}))
    text = ("[META:%s] [%s] %s is prepared in the tray — created %s %s, session `%s` — "
            "one tap to start it." % (aspect, brief_id, clip(title, TITLE_CLIP),
                                      d.get("chip_title_time"), d.get("chip_title_tz"),
                                      task_id))
    return {"ok": True, "poke": {"text": text, "signature": sig}}


def ack(tick: Tick) -> dict:
    """The session sent this tick's pokes: clear `unsent[]` so nothing re-sends."""
    state = dict(tick.last)
    n = len(state.get("unsent") or [])
    state["unsent"] = []
    tick.move("state", "--lane", "dispatch", "--json", json.dumps(state,
                                                                   ensure_ascii=False))
    return {"ok": True, "cleared": n}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="meta-tick", description=__doc__.split("\n")[0])
    ap.add_argument("verb", nargs="?", default="run", choices=("run", "card", "ack"))
    ap.add_argument("id", nargs="?")
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="read-only: dry-run sync/repair/land; no move, launch, "
                         "heartbeat or state write (the comparison tick)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    tick = Tick(".", dry_run=args.dry_run)
    if args.verb == "card":
        if not args.id or not args.task_id:
            ap.error("card needs <id> --task-id <task_id>")
        out = card(tick, args.id, args.task_id)
    elif args.verb == "ack":
        out = ack(tick)
    else:
        out = tick.run()
    if args.json:
        sys.stdout.write(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write((out.get("report") or json.dumps(out, ensure_ascii=False)) + "\n")
    return 0 if out.get("ok", True) else 2


if __name__ == "__main__":
    sys.exit(main())
