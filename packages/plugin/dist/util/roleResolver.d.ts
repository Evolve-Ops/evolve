/**
 * Minimal TS port of the role + capability resolution.
 *
 * Phase C.4 of docs/spec-user-roster-and-roles-2026-06-07.md. Lets
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
export type Role = "admin" | "primary_user" | "participant" | "blocked";
export interface RoleResolution {
    readonly role: Role;
    /** Built-in capability ids granted to this role. */
    readonly capabilities: ReadonlyArray<string>;
    /** Whether the channel-layer flagged this sender as bot owner (informational). */
    readonly isOwner: boolean;
    /** Where the role came from — useful for diagnostics and the LLM injection. */
    readonly resolvedBy: "blocked" | "admin_claim" | "explicit_overlay" | "primary_owner" | "default_participant";
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
export declare function resolveSpeakerRole(botId: string, platform: string, stableId: string, opts?: {
    sharedDir?: string;
}): RoleResolution;
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
export declare function buildSpeakerContextBlock(resolution: RoleResolution, identity: {
    platform: string;
    stableId: string;
    displayName?: string | null;
}): string;
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
export declare function renderDirectoryDigestBlock(digest: DirectoryDigest | null | undefined): string;
//# sourceMappingURL=roleResolver.d.ts.map