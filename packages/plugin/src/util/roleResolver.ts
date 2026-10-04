/**
 * Minimal TS port of the role + capability resolution.
 *
 * Phase C.4 of internal/spec-user-roster-and-roles-2026-06-07.md. Lets
 * TurnObserver inject a per-turn "speaker context" block into the
 * system prompt so the LLM knows who's speaking and what they can do
 * — without that, the bot picks tools blindly and the user gets an
 * awkward "you can't do that" only AFTER the daemon refuses.
 *
 * Why a duplicate of the Python ``roster_overlay.resolve_role`` /
 * ``capabilities.resolve_role_capabilities``: the plugin runs in
 * Node and can't shell out per turn for what is fundamentally a
 * file-read-and-compute. The hot path matters; this resolver reads
 * two small JSON files and does ~5 comparisons. The DAEMON is still
 * the authoritative check — this resolver feeds the LLM hints, not
 * authorization decisions.
 *
 * Source-of-truth invariant: the Python and TS resolvers must agree
 * on the resolved role for a given (overlay, network, identity)
 * tuple. The Python tests pin the rules; this file ports the same
 * rules. Drift between the two would surface as "the LLM thinks
 * I'm admin but the daemon says I'm participant" — annoying but not
 * catastrophic. A future cross-language pin test (read the same
 * fixture, assert both engines agree) is a useful follow-up.
 */

import * as fs from "node:fs";
import * as path from "node:path";


export type Role = "admin" | "primary_user" | "participant" | "blocked";


export interface RoleResolution {
  readonly role: Role;
  /** Built-in capability ids granted to this role. */
  readonly capabilities: ReadonlyArray<string>;
  /** Whether the channel-layer flagged this sender as bot owner (informational). */
  readonly isOwner: boolean;
  /** Where the role came from — useful for diagnostics and the LLM injection. */
  readonly resolvedBy:
    | "blocked"
    | "admin_claim"
    | "explicit_overlay"
    | "primary_owner"
    | "default_participant";
}


/** Match Python ``capabilities.DEFAULT_ROLE_CAPABILITIES``. */
const DEFAULT_ROLE_CAPABILITIES: Record<Role, ReadonlyArray<string>> = {
  admin: [
    "bot.roster.read", "bot.roster.mutate", "bot.roles.bind",
    "bot.channel.config", "bot.config.modify", "bot.app.install",
    "bot.code.modify", "bot.send_external",
  ],
  primary_user: [
    "bot.roster.read", "bot.roster.mutate",
    "bot.channel.config", "bot.config.modify", "bot.send_external",
  ],
  participant: [],
  blocked: [],
};


function overlayPath(sharedDir: string, botId: string): string {
  return path.join(sharedDir, "rosters", `${botId}.json`);
}


function networkPath(sharedDir: string): string {
  return path.join(sharedDir, "network.json");
}


/** Best-effort JSON read; returns null on any failure (missing file,
 *  parse error, perms). Caller treats null as "no overlay state",
 *  which is the correct default for a fresh bot. */
function readJsonOrNull<T = any>(p: string): T | null {
  try {
    return JSON.parse(fs.readFileSync(p, "utf8")) as T;
  } catch {
    return null;
  }
}


/** Every id a person holds on `platform`, tolerant of BOTH live
 *  `external_ids` shapes.
 *
 *  The documented schema is `{channel: [id, ...]}` (invariant 6 — one
 *  person, many platform ids), but legacy per-bot blocks written before
 *  M1-B2 carry a bare string. Reading only one shape is how the two
 *  mirror bugs happened: `Array.isArray` alone silently dropped every
 *  scalar entry, and `String(val)` on a list produced `"111,222"` (or
 *  `"['111']"` on the Python side) and matched nothing.
 *
 *  The TS twin of `evolve_admin.external_ids.ids_for`. */
function externalIdsFor(block: any, platform: string): Set<string> {
  const ext = (block && block.external_ids) || {};
  const raw = ext[platform];
  if (raw === null || raw === undefined) return new Set();
  const list: any[] = Array.isArray(raw) ? raw : [raw];
  return new Set(
    list
      .filter((x) => x !== null && x !== undefined)
      .map((x) => String(x).trim())
      .filter(Boolean),
  );
}


function podAdminIdsFor(network: any, platform: string): Set<string> {
  const pod = (network && network.pod) || {};
  return externalIdsFor(pod.admins, platform);
}


function primaryOwnerIdsFor(
  network: any, botId: string, platform: string,
): Set<string> {
  const bots = (network && network.bots) || {};
  const bot = bots[botId] || {};
  return externalIdsFor(bot.primary_user, platform);
}


function identityKey(platform: string, stableId: string): string {
  return `${platform}:${stableId}`;
}


/**
 * Resolve the role + capability set for a (botId, platform, stableId)
 * triple. Returns the default participant resolution when no overlay or
 * network state suggests otherwise. Never throws; on file errors it
 * resolves as best it can with whatever it could read.
 *
 * Priority chain (mirrors Python ``roster_overlay.resolve_role``):
 *   1. Blocked map in overlay → blocked
 *   2. Pod-admin claim in network.json → admin
 *   3. Explicit overlay role → that role
 *   4. Primary-owner claim in network.json → primary_user
 *   5. Default → participant
 */
export function resolveSpeakerRole(
  botId: string,
  platform: string,
  stableId: string,
  opts: { sharedDir?: string } = {},
): RoleResolution {
  const sharedDir = opts.sharedDir || "/Users/Shared/evolve";
  const overlay = readJsonOrNull(overlayPath(sharedDir, botId));
  const network = readJsonOrNull(networkPath(sharedDir));

  const key = identityKey(platform, stableId);

  // 1. Sticky-deny: blocked.
  const blockedMap = (overlay && overlay.blocked) || {};
  if (key in blockedMap) {
    return {
      role: "blocked",
      capabilities: DEFAULT_ROLE_CAPABILITIES.blocked,
      isOwner: false,
      resolvedBy: "blocked",
    };
  }

  // 2. Pod-admin claim wins next (even over explicit overlay).
  const adminIds = podAdminIdsFor(network, platform);
  if (adminIds.has(stableId)) {
    return {
      role: "admin",
      capabilities: DEFAULT_ROLE_CAPABILITIES.admin,
      isOwner: false,
      resolvedBy: "admin_claim",
    };
  }

  // 3. Explicit overlay role.
  const identities = (overlay && overlay.identities) || {};
  const entry = identities[key];
  if (entry && typeof entry.role === "string") {
    const explicit = entry.role as Role;
    if (
      explicit === "admin" || explicit === "primary_user" ||
      explicit === "participant" || explicit === "blocked"
    ) {
      return {
        role: explicit,
        capabilities: DEFAULT_ROLE_CAPABILITIES[explicit],
        isOwner: false,
        resolvedBy: "explicit_overlay",
      };
    }
  }

  // 4. Primary-owner claim → primary_user.
  if (primaryOwnerIdsFor(network, botId, platform).has(stableId)) {
    return {
      role: "primary_user",
      capabilities: DEFAULT_ROLE_CAPABILITIES.primary_user,
      isOwner: true,
      resolvedBy: "primary_owner",
    };
  }

  // 5. Default.
  return {
    role: "participant",
    capabilities: DEFAULT_ROLE_CAPABILITIES.participant,
    isOwner: false,
    resolvedBy: "default_participant",
  };
}


/**
 * Render the per-turn speaker-context block injected into the LLM's
 * system prompt. Brief — bot system prompts are budget-sensitive.
 *
 * Tells the LLM:
 *   - Who's speaking (platform + stable_id)
 *   - Their effective role on this bot
 *   - Whether they can mutate the roster (the headline capability)
 *   - How to behave: invoke the tool if authorized, decline gracefully if not
 */
export function buildSpeakerContextBlock(
  resolution: RoleResolution,
  identity: { platform: string; stableId: string; displayName?: string | null },
): string {
  const who = identity.displayName
    ? `${identity.displayName} (${identity.platform}:${identity.stableId})`
    : `${identity.platform}:${identity.stableId}`;

  const canMutateRoster = resolution.capabilities.includes("bot.roster.mutate");
  const canConfigChannel = resolution.capabilities.includes("bot.channel.config");

  // The LLM doesn't need the full capability list — it needs the
  // gates that matter for the tools available. Keeping this tight
  // means lower token cost per turn (this fires on every turn for
  // bots where roster tools are registered).
  const gates: string[] = [];
  gates.push(`role=${resolution.role}`);
  gates.push(`can_mutate_roster=${canMutateRoster ? "yes" : "no"}`);
  if (canConfigChannel) {
    gates.push("can_config_channel=yes");
  }

  const tail = canMutateRoster
    ? "If they ask to set a role / block / unblock a user or change a channel's newcomer mode, " +
      "invoke the matching roster_* or channel_set_newcomer_mode tool. " +
      "Use the platform's stable id (Telegram numeric user_id, etc.), NOT the @handle."
    : "If they ask to set a role / block / unblock a user or change channel settings, " +
      "decline politely — they don't have permission. Suggest they contact the bot's admin " +
      "or primary user. Do NOT call the roster_* tools — they will fail.";

  return [
    "SPEAKER (this turn):",
    `  ${who}`,
    `  ${gates.join(" · ")}`,
    "",
    tail,
  ].join("\n");
}


// ── Directory digest (user-directory Phase 3a) ─────────────────────────────

/**
 * One slim digest row — the bot-facing projection of a directory ``Person``.
 *
 * Built server-side by ``user_directory.bot_view._digest_row`` and resolved through
 * ``resolve_persons`` (THE one read path), so these rows cannot diverge from what the
 * admin Users page shows. NB: no behavioral profile and no audit trail ever reach this
 * shape — the server strips them (spec §10 invariant #5).
 */
export interface DirectoryDigestRow {
  readonly person_id: string;
  /** Display name, or "" when unknown. */
  readonly display: string;
  /** "@handle" or "". */
  readonly handle: string;
  /** Stable identity as "<platform>:<id>" (e.g. "telegram:12345"), or "". */
  readonly identity: string;
  /** Primary contact email, or "". */
  readonly primary_email: string;
  /** "admin" | "primary_user" | "participant" | "contact". */
  readonly role: string;
}

/** The digest payload returned by ``GET /api/directory/digest``. */
export interface DirectoryDigest {
  readonly persons: ReadonlyArray<DirectoryDigestRow>;
  /** Count BEFORE the cap — drives the "+N more" overflow line. */
  readonly total: number;
  /** ``persons.length`` (count after the cap). */
  readonly shown: number;
  /** The server-side row cap. */
  readonly cap: number;
}

/**
 * Render the per-turn directory-digest block injected into the LLM's system prompt.
 *
 * Grows the speaker-context injection from "who is speaking now" to a size-bounded
 * index of the bot's admitted roster + named contacts (spec §5 piece 3). Each line is
 * one Person: display name, ``@handle``, stable id, primary email, role.
 *
 * The framing **outranks ``USER.md``** on purpose — IDs/emails the bot may have copied
 * into its own prose are stale or invented; the directory is the canonical store, so the
 * block tells the model to treat it as authoritative. An overflow line points at
 * ``directory_lookup`` for anyone past the cap, so a big roster stays bounded without the
 * model losing the ability to resolve the rest. A closing line steers WRITES to
 * ``directory_upsert`` (Phase 3b) so the model records new/corrected contact details in
 * the canonical store rather than back into ``USER.md`` prose — closing the §0 loop:
 * the digest demotes USER.md for reads, the upsert tool replaces it for writes.
 *
 * Pure + defensive: returns "" for an empty/absent digest (so the caller injects nothing),
 * and never throws — the TurnObserver wraps the socket fetch, this only formats.
 */
export function renderDirectoryDigestBlock(
  digest: DirectoryDigest | null | undefined,
): string {
  const rows = digest?.persons ?? [];
  if (rows.length === 0) return "";

  const lines: string[] = [
    "Roster-verified contacts (AUTHORITATIVE over any names, IDs, or emails in " +
    "USER.md or your own notes — use these exact ids and emails; do not invent or " +
    "hand-type them):",
  ];
  for (const p of rows) {
    // "  • Dana Lopez  @dana  [telegram:12345]  dana@example.net · participant"
    const head: string[] = [p.display || p.handle || p.identity || p.person_id];
    if (p.handle) head.push(p.handle);
    if (p.identity) head.push(`[${p.identity}]`);
    if (p.primary_email) head.push(p.primary_email);
    lines.push(`  • ${head.join("  ")} · ${p.role}`);
  }
  const overflow = Math.max(0, (digest?.total ?? rows.length) - rows.length);
  if (overflow > 0) {
    lines.push(
      `  (+${overflow} more not shown — call directory_lookup(@handle | name | email) ` +
      "to resolve any specific person.)",
    );
  }
  // Steer WRITES to the canonical store, not USER.md prose (spec §5.4 — injection-first
  // USER.md demotion). To record or correct anyone's email/ID/contact details, the model
  // should call directory_upsert — notes hand-written into USER.md are not authoritative,
  // drift, and are invisible to the operator.
  lines.push(
    "To save or correct anyone's email, messaging ID, or contact details, call " +
    "directory_upsert(ref, {emails|identities|contact|names}) — do NOT write contact " +
    "info into USER.md or your notes; only the directory is authoritative and " +
    "operator-visible.",
  );
  return lines.join("\n");
}
