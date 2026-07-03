# Security

## Reporting a vulnerability

Please report potential security issues privately. Two channels, in order of preference:

1. **[GitHub Security Advisories](https://github.com/evolve-ops/evolve/security/advisories/new)** — preferred. Integrates with the repo for disclosure tracking.
2. Email **hello@evolveops.dev** — alternative if you don't have a GitHub account or prefer email.

Do not file public issues for security reports. A maintainer will respond within 7 days to acknowledge the report and discuss next steps.

## Supported versions

Evolve is pre-1.0 and ships from `main`. Only the latest published commit is supported for security fixes. Once a numbered release is cut, that policy will be updated here.

## Disclosure

Once a fix is available, we will coordinate a disclosure timeline with the reporter. Public disclosure typically follows the fix; the reporter is credited unless they request otherwise.

## Scope

In scope: code in this repository, the admin server, the OpenClaw plugin bundled at `packages/plugin/`, and any officially-published install or update flows.

Out of scope: third-party dependencies (report upstream), individual operator misconfigurations, and bots' own conversation content.

## Trust model

The security model rests on a **single-tenant assumption** — the host machine
has no other untrusted local users. That assumption is load-bearing for the
sudoers grants, the `/tmp` staging path, the keystore file permissions, and
the loopback-only admin server.

See [`docs/threat-model.md`](docs/threat-model.md) for the full trust-boundary
documentation, asset inventory, autonomous-apply safety story, and known residual
risks.
