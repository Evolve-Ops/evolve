# Contributing to Evolve

Thanks for your interest. Evolve is pre-1.0 and primarily seeking real-world install reports, bug reports, and documentation feedback at this stage. See [docs/gitpages/CONTRIBUTING.md](docs/gitpages/CONTRIBUTING.md) for the visitor-friendly guide to filing issues and discussion topics.

This document covers code contributions specifically: licensing, sign-off, and PR mechanics. If you're going to work *in* the codebase (not just file the occasional fix), start with **[ONBOARDING.md](ONBOARDING.md)** — the engineering orientation that walks you from clone to a landed PR.

## License

Evolve is licensed under the Business Source License 1.1 — see [LICENSE](LICENSE).

By contributing code, documentation, or other material to this repository, you agree that your contributions are licensed under the same BSL 1.1 terms as the rest of the project.

## Sign-off (DCO — not currently enforced)

Evolve is pre-public and the repo is solo, so we don't currently enforce DCO sign-off on commits. When external contributions open, we may switch to requiring it — at which point this section will describe the policy in detail.

If you want to sign off your commits today anyway (good habit), use `git commit -s` or set `git config --global format.signOff true` once for auto-signing. See the [Developer Certificate of Origin](https://developercertificate.org/) for what the sign-off attests to.

## Pull request flow

1. Open an issue first to discuss the change unless it's a trivial fix.
2. Branch from `main`. Use a descriptive branch name (e.g. `fix/auth-profile-key-shape`).
3. Make focused commits. Each commit should be a logical unit and pass tests.
4. Open a PR with a clear description: what changed, why, how you tested.
5. Be responsive to review.

## Testing

Run the relevant package's tests before opening a PR:

```
cd packages/admin && python3 -m pytest tests/
cd packages/analyzer && python3 -m pytest tests/
```

If your change touches behavior that isn't covered by an existing test, add one.

## Style

Match the surrounding code. We don't enforce a formatter at this stage; readability over consistency.

## Scope of contributions we'll take

At this stage, the highest-value PRs are:

- Bug fixes with a clear reproduction
- Documentation corrections and clarifications
- Tests that codify existing behavior

We are not yet actively accepting feature PRs without prior discussion in an issue. Architectural changes need design conversation upstream of the code.
