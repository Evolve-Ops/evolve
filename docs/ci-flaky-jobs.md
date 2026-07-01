# Known-flaky CI jobs

The **done = CI green** contract (chip-brief preamble in
[`.claude/skills/launch/SKILL.md`](../.claude/skills/launch/SKILL.md), the reconciler
fix-forward brief in [`docs/meta-reconcile-procedure.md`](meta-reconcile-procedure.md))
tells a chip: if the ONLY red check is a known flake listed here, **re-run it
(`gh run rerun <run-id> --failed`) — do not "fix" it.** Never edit code to chase a flake.

A job is listed here only when its red is **non-deterministic** (passes on rerun with no
code change). If a "flaky" job reds *deterministically* on a given PR, that is a real
failure on that PR — fix-forward, don't rerun. When in doubt, rerun once: a green rerun
confirms flake, a second identical red confirms real.

| Job | Symptom | Action |
|-----|---------|--------|
| `smoke (firefox)` (workflow `browser-smoke`) | The firefox engine variant intermittently hits 30s timeouts on every API request when its admin-server subprocess wedges under load — chromium + webkit pass. A red firefox leg with green chromium/webkit is the signature. | Rerun. Don't fix. Only investigate if chromium **and** webkit also red (then it's a real SPA/route regression, not the flake). |
| `Linux e2e (Ubuntu bot deploy/run/admin)` (job `linux-e2e`) | Runner-VM kernel diverges from POSIX.1e ACL semantics (e.g. masked-ACE traverse the VM kernel allows but a real Ubuntu host denies), plus occasional `useradd`/systemd-unit timing races under the ephemeral runner. Intermittent, env-driven, not reproducible on macOS. | Rerun once. If it reds again identically **on a platform-surface change**, treat as real (it gates the Linux port) and escalate — a macOS-only chip that can't reproduce it files a blocker rather than guessing. |

## Maintaining this list

- **Add** a job here only after observing a confirmed non-deterministic red (green on
  rerun, no code change). Record the symptom precisely enough that a chip can tell the
  flake signature from a real failure — a vague "sometimes fails" entry licenses
  ignoring real reds.
- **Remove** a job once its flake is fixed at the source (the durable fix beats a
  standing license to rerun).
- This list is the allow-rerun set, not an allow-ignore set: a flaky job that blocks a
  merge still needs its green rerun queued + noted before the chip declares done.
