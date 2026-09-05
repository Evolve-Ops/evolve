/**
 * appIdentity — the ONE place TypeScript answers "which app is this?".
 *
 * AL-1.4a (internal/build-AL-1.4-app-id-canonical.md §2; decision in
 * internal/design-app-spec-and-discovery-2026-08-15.md §3). `app_id` is the
 * canonical identity: a lowercase slug (`APP_ID_PATTERN` — the same rule
 * `RecordApplicationTool` already enforces), conferred when an app becomes
 * *defined* and immutable thereafter.
 *
 * Its Python twin is
 * `packages/admin/evolve_admin/applications/app_identity.py`; the two are
 * pinned together by the shared vector `tests/fixtures/app-id-resolution.json`
 * (audit-app-framework-2026-07-02 D4). Before this module the order was
 * hand-mirrored in `integrity/appScriptRegistry.ts` and
 * `app_integrity_coverage.py` — a silent drift on either side re-opened the
 * #3387 class (the coverage badge never clears).
 *
 * Resolution order — `app_id` first, then the legacy chain, top-level only:
 *
 *     app_id > pkg_id > id > spec_id > instance_id
 *
 * THE TRIM IS THE D4 DIVERGENCE COLLAPSING. The old `appIdOf` returned the RAW
 * value while Python stripped it, so a manifest whose id carried stray
 * whitespace produced two different coverage keys and the badge could never
 * clear for it. Both sides now strip. The fixture's values are pre-trimmed, so
 * no existing case changes answer — but the quirk it called out is gone.
 *
 * `unknown` stays the no-id fallback here (the middleware's sentinel: it is
 * never a match, because several id-less manifests would otherwise collapse
 * onto one key and "prove" coverage for each other). Python's twin returns ""
 * for the same case — each side keeps its own documented fallback.
 */
/** Lowercase alphanumeric + hyphens, 3-48 chars, alphanumeric at both ends. */
export const APP_ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$/;
/** The pre-1.4 chain. Order is load-bearing; mirrors app_identity.py. */
export const LEGACY_APP_ID_FIELDS = [
    "pkg_id",
    "id",
    "spec_id",
    "instance_id",
];
/** The 1.4a order: canonical field first, legacy chain as fallback. */
export const APP_ID_FIELD = "app_id";
export const APP_ID_RESOLUTION_ORDER = ["app_id", ...LEGACY_APP_ID_FIELDS];
/** The middleware's sentinel for a manifest that declared no identity. */
export const NO_APP_ID = "unknown";
function firstNonEmpty(manifest, fields) {
    const m = manifest;
    if (!m || typeof m !== "object")
        return "";
    for (const k of fields) {
        const v = m[k];
        if (typeof v === "string" && v.trim())
            return v.trim();
    }
    return "";
}
/**
 * The canonical app id for a **raw** manifest; `"unknown"` when none resolves.
 *
 * Feed the RAW manifest on disk — the admin's v7-arc hydration rewrites
 * `id`/`pkg_id`, so a hydrated manifest can resolve to a different key than
 * the one written into the coverage file.
 */
export function appIdOf(manifest) {
    // The `app_id` FIELD only wins when its value is a conforming slug. The name
    // was not free: gallery/index.json has carried an `app_id` key since #3413
    // holding the app SCRIPT name (`app_task_manager`), not the package key, and
    // all 15 builtin packages' values fail APP_ID_PATTERN. Honoring those would
    // hand every gallery reader an id that the stamp side refuses to write, so a
    // non-conforming value falls through to the legacy chain — exactly what those
    // readers resolved to before 1.4a. Mirrors resolve_app_id in app_identity.py.
    const canonical = firstNonEmpty(manifest, [APP_ID_FIELD]);
    if (canonical && isCanonicalAppId(canonical))
        return canonical;
    return firstNonEmpty(manifest, LEGACY_APP_ID_FIELDS) || NO_APP_ID;
}
/**
 * The scanner-assigned draft id, or `""` — NEVER falls back to an app id.
 *
 * A discovered draft's identity is explicitly unstable (design §3): the
 * scanner may merge, rename or drop it freely, and it must never appear in
 * attribution, access or sharing. Falling back to the legacy chain here would
 * hand callers exactly the durable-looking id a draft is not allowed to have.
 */
export function draftIdOf(manifest) {
    return firstNonEmpty(manifest, ["draft_id"]);
}
/** True when `value` is already a conforming, lowercase app-id slug. */
export function isCanonicalAppId(value) {
    if (typeof value !== "string")
        return false;
    const t = value.trim();
    return t === t.toLowerCase() && APP_ID_PATTERN.test(t);
}
//# sourceMappingURL=appIdentity.js.map