# PM Inbox — GitHub token setup

PM Inbox authenticates to GitHub with **fine-grained personal access tokens**.
The token scopes are the app's trust boundary: they make code writes, merges,
and private-source reads structurally impossible, regardless of what the LLM
is asked to do. Mint them exactly as below — never substitute a classic PAT.

A fine-grained PAT is scoped to a **single resource owner**. If your public
repo and private tracker have different owners (an org and your user account,
say), you need **two** tokens.

## 1. Mint the tokens

GitHub → Settings → Developer settings → Fine-grained tokens → Generate new:

**Token A — public repo** (resource owner = the public repo's owner; if that
is an organization, the org must allow fine-grained PATs under Organization
settings → Third-party access → Personal access tokens):

- Repository access: only your public repo.
- Permissions: **Issues: Read and write** · **Pull requests: Read-only** ·
  **Contents: Read-only** · Metadata: Read-only (mandatory).

**Token B — private tracker** (resource owner = the private repo's owner):

- Repository access: only your private tracker repo.
- Permissions: **Issues: Read and write** · Metadata: Read-only (mandatory).
- **Do not grant Contents** — the app must not be able to read private source.

Set an expiry you'll actually rotate (90 days is sane; calendar it).

Skip Token B entirely if you leave `private_repo` empty in the app config —
private filing is then disabled and only Token A is needed.

## 2. Store them (bot-owned, mode 0600, outside the workspace)

One JSON file at `~/.openclaw/pm-inbox-github-tokens.json` **in the bot's
home**, owned by the bot user, mode 0600:

```json
{ "public": "github_pat_…", "private": "github_pat_…" }
```

As the pod admin (Linux example; `darwin` = your bot's user):

```bash
sudo install -o darwin -g darwin -m 600 /dev/null /home/darwin/.openclaw/pm-inbox-github-tokens.json
sudo tee /home/darwin/.openclaw/pm-inbox-github-tokens.json >/dev/null <<'EOF'
{ "public": "github_pat_…", "private": "github_pat_…" }
EOF
sudo chmod 600 /home/darwin/.openclaw/pm-inbox-github-tokens.json
```

The file lives under `~/.openclaw/` (bot-private), **not** under
`workspace/` — workspace subtrees can be group-read-widened as the
admin↔bot shared channel, and a token must never sit there. The sweep
refuses to run (`PM_INBOX_FAILED: tokens-missing-or-insecure`) if the file
is missing or its mode is looser than 0600.

## 3. Labels

Issues RW can apply labels but creating label *definitions* is repo-admin
surface. While signed in, create on the public repo: `bug`, `feature`,
`question`, `support`, `spam`, `duplicate`; and on the private tracker:
`from-public` (plus any `<aspect>:from-public` hints you configure).
