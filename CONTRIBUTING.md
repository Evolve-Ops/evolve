# Contributing to Evolve

Thanks for your interest. Evolve is pre-1.0 and primarily seeking real-world install reports, bug reports, and documentation feedback at this stage. See [docs/gitpages/CONTRIBUTING.md](docs/gitpages/CONTRIBUTING.md) for the visitor-friendly guide to filing issues and discussion topics.

## How this repository works — read this before opening a PR

Evolve is developed in a **private repository**. This public repository is a
**per-release curated snapshot** of it: at every release the maintainers
publish a fresh, scrub-gated copy of the tree as a **single squashed commit**
that replaces the previous snapshot. There is **no persistent public history**
— the public repo always contains exactly one commit, and each release
force-replaces it by design.

What that means in practice:

- **Issues are the front door — and they're very welcome here.** Bug reports,
  install reports, feature requests, and documentation feedback filed on this
  repo are read and triaged by the maintainers. When a fix ships, the issue is
  closed with a comment naming the release that contains it.
- **External PRs cannot be merged directly.** A PR opened here targets a
  snapshot that will be replaced wholesale at the next release, so GitHub's
  merge button is never used. Instead, a good patch is **re-applied by the
  maintainers in the private repo with attribution** (credited in the commit
  and/or release notes), and ships in the next public snapshot. If you want to
  contribute code, a small focused diff attached to an issue — or a PR treated
  as a reviewable patch — works equally well.
- **Don't build on top of public history.** Because each release replaces the
  single public commit, long-lived forks will not fast-forward. Rebase your
  fork onto each new snapshot instead.

## License

Evolve is licensed under the Business Source License 1.1 — see [LICENSE](LICENSE).

By contributing code, documentation, or other material (including patches
re-applied from public PRs or issues), you agree that your contributions are
licensed under the same BSL 1.1 terms as the rest of the project.

## Sign-off (DCO — not currently enforced)

We don't currently enforce DCO sign-off. When external contributions grow, we
may switch to requiring it — at which point this section will describe the
policy in detail.

If you want to sign off your commits today anyway (good habit), use `git commit -s` or set `git config --global format.signOff true` once for auto-signing. See the [Developer Certificate of Origin](https://developercertificate.org/) for what the sign-off attests to.

## Filing a good patch

1. Open an issue first describing the problem — this is what actually gets
   triaged, and it gives your patch a home even after the snapshot rotates.
2. Keep the diff focused: one logical change, against the current snapshot.
3. Say how you tested it.
4. If it's accepted, the maintainers re-apply it privately with attribution
   and it ships in the next release; the issue is closed with the release name.

## Testing

Run the relevant package's tests before proposing a change:

```
cd packages/admin && python3 -m pytest tests/
cd packages/analyzer && python3 -m pytest tests/
```

If your change touches behavior that isn't covered by an existing test, add one.

## Style

Match the surrounding code. We don't enforce a formatter at this stage; readability over consistency.

## Scope of contributions we'll take

At this stage, the highest-value contributions are:

- Bug reports with a clear reproduction
- Bug-fix patches attached to those reports
- Documentation corrections and clarifications
- Tests that codify existing behavior

We are not yet actively accepting feature patches without prior discussion in an issue. Architectural changes need design conversation upstream of the code.
