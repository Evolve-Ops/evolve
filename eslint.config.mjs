// ESLint — Phase 6.3 toolchain ratchet for the admin SPA's JavaScript.
//
// Scope: packages/admin/evolve_admin/web (static/js + sw.js). The plugin
// package is TypeScript and gated by `tsc --noEmit` in CI; the dashboard/
// gallery packages are not part of the admin runtime.
//
// Baseline mechanism: ESLint's native bulk suppressions
// (eslint-suppressions.json) — same ratchet semantics as
// tools/except-pass-lint: legacy violations are suppressed, new code must
// be clean, and the suppressions file only shrinks (regenerate with
// `npx eslint --fix-dry-run --suppress-all` only when deliberately
// re-freezing; prune with `--prune-suppressions` after fixing).
//
// no-undef is OFF by design: these are classic same-global-scope scripts
// loaded in order via <script> tags (api.js defines `api`, pages call it).
// Enforcing no-undef would require maintaining a hand-written inventory of
// every cross-file global — all noise, no defect signal. If the SPA ever
// migrates to ES modules, flip it back on.

import js from "@eslint/js";

export default [
    {
        files: ["packages/admin/evolve_admin/web/**/*.js"],
        ...js.configs.recommended,
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: "script",
            globals: {
                // Browser
                window: "readonly",
                document: "readonly",
                navigator: "readonly",
                fetch: "readonly",
                localStorage: "readonly",
                sessionStorage: "readonly",
                console: "readonly",
                setTimeout: "readonly",
                clearTimeout: "readonly",
                setInterval: "readonly",
                clearInterval: "readonly",
                requestAnimationFrame: "readonly",
                URL: "readonly",
                URLSearchParams: "readonly",
                AbortController: "readonly",
                FormData: "readonly",
                Blob: "readonly",
                FileReader: "readonly",
                TextDecoder: "readonly",
                TextEncoder: "readonly",
                CustomEvent: "readonly",
                EventSource: "readonly",
                WebSocket: "readonly",
                Notification: "readonly",
                MutationObserver: "readonly",
                IntersectionObserver: "readonly",
                ResizeObserver: "readonly",
                history: "readonly",
                location: "readonly",
                alert: "readonly",
                confirm: "readonly",
                prompt: "readonly",
                getComputedStyle: "readonly",
                matchMedia: "readonly",
                crypto: "readonly",
                atob: "readonly",
                btoa: "readonly",
                structuredClone: "readonly",
                // Service worker (sw.js)
                self: "readonly",
                caches: "readonly",
                clients: "readonly",
                registration: "readonly",
            },
        },
        rules: {
            ...js.configs.recommended.rules,
            // See header comment — shared-global-scope architecture.
            "no-undef": "off",
            // Same rationale, write side: page modules intentionally define
            // functions/consts consumed by other files or inline handlers,
            // which recommended's no-unused-vars can't see. Keep it on for
            // genuinely-local noise but allow leading-underscore escapes.
            "no-unused-vars": ["error", {
                args: "none",
                varsIgnorePattern: "^_",
                // House convention: `catch (_e)` / `catch (_)` marks an
                // intentional discard.
                caughtErrorsIgnorePattern: "^_",
            }],
        },
    },
];
