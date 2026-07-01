# Contract: the app-result block — Evolve's execution-integrity floor (hook-upgradeable)

**Status:** ACTIVE. Shipped as the forge-baked floor; designed to be the exact
shape a future OpenClaw tool-exec hook reads, so the hook snaps on top later
with no format change and no rebuild.
**Aspect:** META:apps.
**Date:** 2026-06-30.
**Spec:** [spec-app-invocation-just-works-2026-06-29.md](spec-app-invocation-just-works-2026-06-29.md)
§2.2 (the integrity layer).
**Re-resolution of OQ-2:** [blocker-app-integrity-harness-oq2-2026-06-30.md](blocker-app-integrity-harness-oq2-2026-06-30.md)
— the gateway-shim hook does not exist in OpenClaw, and **Evolve never forks
OpenClaw** (it is an external public-npm product Evolve admins, not owns — see
the memory note *evolve-is-admin-layer-on-uncontrolled-openclaw*). So OQ-2 is
re-resolved to the **forge-baked** option: the strongest integrity Evolve can
deliver from the layer it 100% controls — the forge.
**Code:** [packages/admin/evolve_admin/applications/app_result.py](../packages/admin/evolve_admin/applications/app_result.py)
(contract + parser + launcher source);
[forge_engine.\_bake_result_harness](../packages/admin/evolve_admin/applications/forge_engine.py)
(forge integration).

---

## 1. What it is

When a forged app's command runs, it runs through a **self-reporting launcher**
(`evolve/app_result/run.py`, written into the bot by the forge). The launcher
runs the real command, captures its **true** exit code + stdout/stderr (the
proven capture shape is
[`oc_cli._run_oc_subprocess`](../packages/analyzer/oc_cli.py)), and emits one
**machine-parseable, sentinel-delimited result block** to stdout. The assistant
narrates from that block instead of inventing an outcome.

```
<<<<<EVOLVE_APP_RESULT/1
{"schema":"evolve.app_result/1","status":"ok|failed","exit_code":0,"timed_out":false,"error_summary":null,"stdout":"...","stderr":"...","stdout_truncated":false,"stderr_truncated":false,"duration_ms":123,"command":["python3","scripts/tasks.py","add"]}
EVOLVE_APP_RESULT/1>>>>>
```

* **Sentinels are whole lines.** The block is `OPEN` line, one single-line JSON
  object, `CLOSE` line.
* **`status`** is `ok` **iff** the command exited 0 and did not time out; else
  `failed`.
* **`exit_code`** is the real exit code of the wrapped command (`null` if it
  could not be started).
* **`error_summary`** is a plain-language failure line on `failed` (`null` on
  `ok`) — what the assistant tells the user, never a raw traceback.
* **`stdout` / `stderr`** are the captured child streams (decoded with
  replacement for non-UTF8, capped at `STREAM_CAP` with a `*_truncated` flag).
* **`command`** is the wrapped argv — so a reader can map the result back to a
  known app's `interface_contract.cli` / `realized_files`.

`format_result_block` is the canonical serializer; the launcher's `_emit`
mirrors it byte-for-byte (pinned by a round-trip test).

## 2. Fail-closed parsing (the integrity guarantee)

`parse_result_block(captured) -> AppResult` is the single canonical reader. Its
whole job is to make "I can't tell" resolve to **failure**, never success:

| Input | Verdict | `reason` |
|---|---|---|
| Well-formed `ok`, exit 0 | success | `""` |
| Well-formed `failed` | failure | `""` |
| **No block** (script crashed before emit / killed / never wrapped) | failure | `absent` |
| **>1 block** (tampering / bug) | failure | `ambiguous` |
| Unparseable JSON / missing or bad `status` | failure | `malformed` |
| `status: ok` but non-zero exit or timeout | failure | `contradictory` |

`AppResult.is_success` is the one question callers ask; it is True only for a
clean `ok`. Tolerates non-UTF8 bytes and arbitrary leading/trailing noise.

## 3. Injection resistance

A hostile or buggy child cannot forge a second block: the launcher embeds the
child's captured stdout as a **JSON string value**, so any sentinel-looking
bytes the child printed are escaped inside a quoted string and never appear as a
bare sentinel line. The launcher writes nothing else to its own stdout (the
child's raw stdout is captured, never passed through), so exactly one authentic
block exists — and it carries the child's real exit code. The end-to-end tests
exercise this with a child that prints a perfectly-formed fake `ok` block and
then exits non-zero; the verdict is `failed`.

**Producer-trust boundary.** The integrity here is a property of the
*producer*, not of content inspection: the launcher is the **sole emitter of
the sentinel delimiter lines** and **captures the wrapped command's output
out-of-band** (it never shares its stdout fd with the child). `parse_result_block`
is safe on output from such a producer. It deliberately does **not** authenticate
the delimiter lines — a legitimate block's `stdout`/`stderr` field may itself
contain the sentinel token (the launcher preserves real output), so a parser
cannot tell a launcher-emitted delimiter from an attacker-supplied one by
content alone. Therefore a consumer must only ever parse blocks it captured from
the wrapper / tool boundary, never raw attacker-controlled channel text. The OC
hook upgrade (§5) preserves this exactly — it captures the tool's stdout
out-of-band and emits the block itself — so the un-fakeable contract is
unaffected. Reusing `parse_result_block` to scrape blocks out of arbitrary chat
text would step outside this boundary and is not supported.

## 4. The honest residual (what this is NOT)

This is a **floor**, not the un-fakeable contract. Without the OpenClaw hook
(§5), the launcher only sees the command **if the assistant runs it through the
launcher** — and a bot that bypasses the wrapper, or whose process is killed
before the block is emitted, can still misreport. The parser's fail-closed rule
bounds that residual; it does not erase it. This raises the bar a lot —
confabulating success now means contradicting a visible structured `failed`
block — but it is not the "the bot cannot fake it" guarantee §2.2 reserves for a
real hook.

Consequences kept honest:

* **The "⚠ May misreport failures" badge stays firing.** Integrity is improved,
  not un-fakeable, so clearing it would be dishonest
  ([principle-alerts-explain-and-remediate.md](principle-alerts-explain-and-remediate.md)).
  Re-homing the badge is a separate follow-on (spec §3, Bite 2).
* **The post-hoc backstop stays the truth-teller.**
  [`app_script_failure_audit.py`](../packages/analyzer/app_script_failure_audit.py)
  still counts `(agent) failed` chips across sessions. Because the launcher
  exits 0 after emitting its block (replacing the raw `(agent) failed` exec chip
  with a clean structured failure), that audit should find **less** once apps
  self-report — exactly the intent of §2.2. It remains the backstop that proves
  the floor is working.

## 5. How the OpenClaw hook snaps on top later (no rebuild)

The missing primitive (blocker doc §6) is a synchronous tool-exec hook —
`before_tool_call` / `after_tool_call` / a `PreToolUse`/`PostToolUse` shell hook
Evolve could inject into `openclaw.json`. When it lands:

1. The hook sees the resolved command + real exit/stdout/stderr **out of band**
   from the model — exactly what the launcher captures today, but the model
   cannot bypass it.
2. The hook emits **this same block** via `format_result_block` (or the TS
   equivalent reading the same shape) and **substitutes** the structured outcome
   into what the channel sees. `parse_result_block` reads it unchanged.
3. The assistant's narration is then *derived* from the block with no way to
   bypass — the un-fakeable §2.2 contract.

Nothing about the block changes. The launcher becomes redundant where the hook
runs (or stays as belt-and-suspenders); the bot_guidance flips from "run via the
wrapper" to "the system handles it." **Floor now, hook on top later, no format
change.** This is the concrete meaning of "roll with OpenClaw, never fork": we
shipped the strongest floor on the substrate we control, shaped so the upstream
capability — if it ever lands — is a *promotion*, not a rewrite.

## 6. Scope of the forge-baked floor

* **Covered:** newly forged / re-forged apps. `_bake_result_harness` writes the
  launcher (idempotent, under `workspace/evolve/` which carries the evolve-user
  write ACL — no bot-owned-file write) and merges the honesty `bot_guidance`
  section on every forge apply.
* **Not covered here (follow-ons):** retrofitting already-installed apps' on-disk
  scripts (needs the privileged bot-owned-file write helper from the concurrent
  marker-stamp chip); re-homing the badge (spec §3 / Bite 2); the OpenClaw hook
  itself (external, filed upstream). Existing apps get the harness on their next
  re-forge until the retrofit lands.
