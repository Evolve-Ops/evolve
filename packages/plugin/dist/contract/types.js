/**
 * The OpenClaw compatibility contract — shared types.
 *
 * Every assumption the Evolve plugin makes about OpenClaw is a *check* in
 * this contract. The contract is run against a REAL installed OpenClaw (a
 * versioned prefix, or an npm `--prefix` install in CI) before Evolve ever
 * offers that version to an operator.
 *
 * Why this exists: internal/design-oc-upgrade-safety-2026-09-08.md §2 row 3.
 * On 2026-09-07 a single Update-card click moved nine bots from OC 2026.7.1
 * to 2026.9.2. Every Evolve-side failure that followed was an assumption a
 * point release had changed without anyone checking — that
 * `before_model_resolve` does not fire on the plugin's own subagent
 * sessions, that a bare silent token is treated as silence, that a set of
 * config keys is still valid, that `plugins install -l` needs only
 * `--force`. None of them are visible in a changelog at the level Evolve
 * depends on. The only defence that scales past this one pod is to test the
 * assumptions against the target version first.
 *
 * Every check therefore names the incident that motivated it (`incident`),
 * so a failure reads as "this is the thing that broke last time", not as an
 * anonymous red row.
 *
 * READ-ONLY BY CONSTRUCTION. A check may run the OpenClaw CLI, read the
 * installed package, and validate a config inside a throwaway HOME. It must
 * never pass `--fix`, never write outside that temp HOME, and never touch a
 * live bot — the nightly matrix run executes this suite unattended.
 */
export {};
//# sourceMappingURL=types.js.map