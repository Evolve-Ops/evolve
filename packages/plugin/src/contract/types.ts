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

/** Outcome of a single contract check. */
export type CheckStatus = "pass" | "fail" | "skip";

/** One check's verdict, plus the evidence that produced it. */
export interface CheckResult {
  /** Stable kebab-case id — this is what a matrix row and a preflight blocker name. */
  id: string;
  /** One line an operator can read. */
  title: string;
  /** The internal/ document describing the incident this check exists for. */
  incident: string;
  status: CheckStatus;
  /** Why it landed that way, phrased for an operator. */
  detail: string;
  /** Raw observations behind the verdict (command output excerpts, matched keys). */
  evidence: Record<string, unknown>;
}

/** A check: pure over the probe, so the same code runs against a fake. */
export interface ContractCheck {
  id: string;
  title: string;
  incident: string;
  run(probe: OcProbe): Promise<CheckResult>;
}

/** Result of `openclaw config validate` on a candidate config. */
export interface ConfigValidation {
  /** True iff the validator exited clean. */
  ok: boolean;
  /** Every diagnostic line the validator emitted, in order. */
  messages: string[];
  /** Unparsed stdout+stderr, for evidence. */
  raw: string;
}

/** Result of a read-only CLI invocation. */
export interface CliResult {
  code: number;
  stdout: string;
  stderr: string;
}

/**
 * The seam every check talks to.
 *
 * Two implementations exist: `createCliProbe` (a real installed OpenClaw)
 * and the fakes in `tests/contract.*.test.mjs` (one that behaves like a
 * healthy runtime, one that reproduces the 2026-09-07 incident). A check
 * that cannot be answered — the CLI is missing, the package is unreadable —
 * returns `skip`, and a run with any skip is NOT a pass.
 */
export interface OcProbe {
  /** The OpenClaw version under test, or null when it could not be read. */
  version(): Promise<string | null>;
  /**
   * False when the openclaw binary could not be executed at all.
   *
   * Load-bearing, and learned the hard way in this module's first dry run:
   * without it, a machine with NO OpenClaw ran the CLI-backed checks, got
   * "command not found" back, and three of them reported PASS — the exact
   * shape of "we could not tell" masquerading as validation. Every
   * CLI-dependent check consults this first and skips.
   */
  cliAvailable(): Promise<boolean>;
  /** Run a READ-ONLY openclaw CLI invocation inside the probe's temp HOME. */
  cli(args: string[]): Promise<CliResult>;
  /**
   * An absolute path to a directory inside the probe's throwaway HOME,
   * created if it does not exist.
   *
   * Exists because the target validates that some config VALUES point at
   * real paths: the first real CI run pointed `plugins.load.paths` at the
   * pod's own plugin dir, got "plugin path not found", and reported an
   * environmental fact as a retired key.
   */
  scratchDir(name: string): Promise<string>;
  /** Write a candidate openclaw.json into the probe's temp HOME. */
  stageConfig(config: unknown): Promise<void>;
  /** Stage `config`, then run `openclaw config validate` over it. */
  validateConfig(config: unknown): Promise<ConfigValidation>;
  /** False when the installed package could not be read at all (checks skip). */
  runtimeAvailable(): Promise<boolean>;
  /** True iff `needle` occurs anywhere in the installed runtime's shipped files. */
  runtimeMentions(needle: string): Promise<boolean>;
  /** Read a file from the Evolve REPO (repo-root-relative path). */
  evolveSource(repoRelPath: string): Promise<string | null>;
}

/** One full contract run against one OpenClaw version. */
export interface ContractRun {
  /** The OpenClaw version the contract ran against, or null if unreadable. */
  ocVersion: string | null;
  /** The Evolve plugin version that owns these assumptions. */
  evolveVersion: string;
  startedAt: string;
  durationMs: number;
  /**
   * True only when EVERY check passed. A skip is not a pass: the Update
   * card reads `ok` to decide whether a version is offered at all, so an
   * unanswerable check must leave the version un-validated.
   */
  ok: boolean;
  results: CheckResult[];
}
