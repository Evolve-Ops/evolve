"""bot_credential_inventory — what is still *live* in a bot account's home.

An inventory of the token-bearing artifacts a bot account carries, reported by
**location and shape only — never by value**. Two callers share it, which is
the whole reason it is its own module rather than a private helper in either:

  * ``orphaned_bot_account_monitor`` — an account whose Evolve install no
    longer backs a roster member. The Signal has to say exactly what is still
    exposed, or the operator cannot judge urgency.
  * ``evolve_admin.retire.generate_closure_summary`` — the same list, at the
    moment of retirement, so the operator knows what to revoke by hand *before*
    the account goes quiet rather than seven weeks later.

Both want the identical answer to "what secrets does this account still hold",
and a drift between the two would be worse than useless: the closure summary
would promise a list the monitor later contradicts.

The value-free contract
=======================

Nothing in this module ever puts a secret's *value* into a return value, a log
line, a Signal body, or a Markdown summary. What callers get is the dotted
config path (``channels.telegram.botToken``) or the file path, plus a shape
note (``"46 chars"``, ``"private key file"``). That is enough to (a) recognize
which credential it is and (b) go revoke it at the source, and it is safe to
put in a Signal body that gets mirrored into operator chat and the Alerts UI.

The one judgement call: a secret's *length* is reported. A length is not a
value and does not narrow a brute force in any useful way, but it does let an
operator tell "the field is present and populated" from "the field is present
but empty" — which is the difference between a live credential and a stub, and
therefore between an urgent revoke and a no-op.

Reads and the fail-safe
=======================

Reads go direct first (the evolve ACL that ``deploy.set_evolve_read_acl``
maintains) and fall back to the granted ``sudo <cat|ls>`` argv forms —
``{home}/*/.openclaw/openclaw.json`` and ``{home}/*/.ssh`` are both already in
``_render_evolve_sudoers``, so this module adds **no new sudoers grant** and
needs no ``refresh-sudoers`` run to work on a live pod (which matters: the
refresh is manual by design, so a monitor that needed a new grant would ship
dead until an operator noticed).

Every path that cannot be read is recorded in :attr:`Inventory.unread` rather
than being silently dropped. A credential inventory that cannot distinguish
"no secrets here" from "could not look" is actively dangerous — it is the exact
shape that would tell an operator an account is clean when it is not. Callers
are expected to surface ``unread`` alongside ``artifacts``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platform_profile import get_profile

# ── What counts as a secret-shaped config field ────────────────────────────

# Substrings that make a JSON key name secret-shaped, matched case-insensitively
# against the key. Deliberately broad — a false positive costs the operator one
# glance at a named field, a false negative leaves a live credential unlisted.
_SECRET_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "credential",
    "privatekey",
    "private_key",
    "signingkey",
    "signing_key",
    "accesskey",
    "access_key",
)

# Key names that MATCH a substring above but are not secrets — model/context
# budget knobs (``maxTokens``, ``tokenLimit``) and the tokenizer id. The int/str
# and length gates below already exclude the numeric ones; these are listed so
# a future string-valued sibling can't sneak in.
_SECRET_KEY_DENYLIST: frozenset[str] = frozenset(
    {
        "maxtokens",
        "maxoutputtokens",
        "tokenlimit",
        "tokenizer",
        "tokencount",
        "contexttokens",
    }
)

# Minimum length for a string value to read as a live credential. Real tokens
# are long (a Telegram bot token is ~46 chars, an OC gateway token ~48); short
# strings under a secret-shaped key are almost always a mode/name ("none",
# "env", "bearer"). Reported-but-empty is still worth knowing, so a value that
# is present-but-short is classified rather than dropped.
_MIN_SECRET_LEN = 16

# Value prefixes that mean "this is a reference, not the secret itself" — OC
# supports env indirection, and a config that only names an env var is NOT a
# credential sitting on disk. Classified separately so the operator is not sent
# to revoke something that was never there.
_INDIRECTION_PREFIXES: tuple[str, ...] = ("${", "env:", "$(")

# Depth bound on the config walk. Real nesting (``channels.discord.guilds.
# <gid>.…``) tops out around 5; 8 leaves headroom without letting a
# pathological config spin.
_MAX_WALK_DEPTH = 8

# Where to go to revoke a channel credential.
#
# channel-literal-allow-begin: this is NOT a channel enumeration and must not
# become one. Nothing here decides WHICH channels get checked — the harvest
# walks the whole config by key name, so a platform the registry has never
# heard of is still reported (see ``test_harvest_covers_an_unknown_channel``).
# What this maps is revocation DESTINATIONS: the specific website an operator
# opens to invalidate a token. That is per-platform product knowledge with no
# registry column today, it cannot be derived from any capability flag, and a
# guess would send someone to the wrong console. An id absent from this map
# degrades to the generic "revoke in the <Label> developer console", with the
# label read from ``channel_registry`` — so adding a platform to the registry
# keeps working here with no edit, which is the invariant-7 property that
# matters.
_CHANNEL_REVOKE_HINTS: dict[str, str] = {
    "telegram": "BotFather → /mybots → the bot → API Token → Revoke",
    "slack": "api.slack.com/apps → the app → OAuth & Permissions → Regenerate",
    "discord": "discord.com/developers → the app → Bot → Reset Token",
    "whatsapp": "the Meta app dashboard → WhatsApp → API Setup → regenerate",
    "signal": "re-link the Signal device (unlink the old one in Linked Devices)",
}
# channel-literal-allow-end

# Filenames under ~/.ssh that are NOT private keys.
_SSH_NON_KEY_NAMES: frozenset[str] = frozenset(
    {"known_hosts", "known_hosts.old", "config", "authorized_keys", "environment"}
)


# ── Result types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CredentialArtifact:
    """One live credential, described without its value.

    ``location`` is a dotted config path or a filesystem path, ``shape`` says
    what was found (``"46 chars"`` / ``"private key file"``), and ``revoke_with``
    is the operator instruction. ``live`` is False for artifacts that are
    present but not actually a secret on disk (an empty field, an env
    indirection) — those are reported so the operator can see they were
    considered and dismissed, not silently omitted.
    """

    location: str
    kind: str
    shape: str
    revoke_with: str
    live: bool = True


@dataclass
class Inventory:
    """The full picture for one account: what was found, and what could not be read."""

    artifacts: list[CredentialArtifact] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)

    @property
    def live_artifacts(self) -> "list[CredentialArtifact]":
        """Just the artifacts that are genuinely live credentials on disk."""
        return [a for a in self.artifacts if a.live]

    @property
    def is_blind(self) -> bool:
        """True when at least one expected location could not be read.

        A caller must not report "nothing to revoke" while this is True.
        """
        return bool(self.unread)


# ── Reads (ACL first, granted-sudo fallback) ──────────────────────────────


def _probe(path: Path) -> str:
    """``"present"`` / ``"absent"`` / ``"unreadable"`` for *path*.

    Uses ``os.lstat`` rather than ``Path.exists()`` on purpose: since Python
    3.13 ``exists()`` swallows EACCES and returns False, so an unreadable path
    is indistinguishable from a missing one — and for this module that would
    turn "I could not check for credentials" into "there are no credentials",
    which is the single worst way this code could be wrong.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    return "present"


def read_openclaw_config(home: Path) -> "tuple[dict[str, Any] | None, str]":
    """``(config, status)`` for ``<home>/.openclaw/openclaw.json``.

    ``status`` is ``"ok"``, ``"absent"``, or a short failure reason. Direct read
    first (the evolve ACL); ``sudo <cat>`` is the fallback, matching the
    ``{home}/*/.openclaw/openclaw.json`` grant exactly.
    """
    path = home / ".openclaw" / "openclaw.json"
    text: "str | None" = None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        try:
            r = subprocess.run(
                ["sudo", get_profile().cat, str(path)],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return None, f"sudo cat failed: {type(exc).__name__}"
        if r.returncode != 0:
            if "No such file" in (r.stderr or ""):
                return None, "absent"
            return None, "unreadable (EACCES even via the sudo-cat fallback)"
        text = r.stdout
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"unparseable JSON: {exc.msg}"
    if not isinstance(data, dict):
        return None, "unexpected shape (not a JSON object)"
    return data, "ok"


def _list_dir(path: Path) -> "tuple[list[str] | None, str]":
    """``(names, status)`` for a directory, ACL read then ``sudo <ls>``.

    ``sudo ls`` is the fallback because ``~/.ssh`` sits OUTSIDE the ACL'd
    ``.openclaw`` tree; ``{home}/*/.ssh`` is an existing grant.
    """
    try:
        return sorted(p.name for p in path.iterdir()), "ok"
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        # Kept, not swallowed: if the sudo fallback ALSO fails, the direct
        # error is the more informative half of the story (it names the ACL
        # problem; the sudo leg only reports a missing grant).
        direct_error = f"{type(exc).__name__}: {exc}"
    try:
        r = subprocess.run(
            ["sudo", get_profile().ls, str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"{direct_error}; sudo ls also failed: {type(exc).__name__}"
    if r.returncode != 0:
        if "No such file" in (r.stderr or ""):
            return None, "absent"
        return None, f"{direct_error}; unreadable via the sudo-ls fallback too"
    return sorted(n for n in r.stdout.split() if n), "ok"


# ── Config-field harvest (pure) ───────────────────────────────────────────


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SECRET_KEY_DENYLIST:
        return False
    return any(s in lowered for s in _SECRET_KEY_SUBSTRINGS)


def _classify_value(value: Any) -> "tuple[str, bool]":
    """``(shape, live)`` for a secret-shaped key's value."""
    if not isinstance(value, str):
        # A non-string under a secret-shaped key is a budget knob or a
        # structured sub-block, not a credential.
        return "", False
    stripped = value.strip()
    if not stripped:
        return "empty — nothing to revoke", False
    if stripped.startswith(_INDIRECTION_PREFIXES):
        return "environment reference — no secret stored on disk", False
    if len(stripped) < _MIN_SECRET_LEN:
        return f"{len(stripped)} chars — too short to be a live token", False
    return f"{len(stripped)} chars", True


def _revoke_hint_for(dotted_path: str) -> str:
    """Operator instruction for a config-field credential.

    Channel tokens get a platform-specific destination; everything else gets
    the honest generic answer (rotate at the issuing provider), because
    inventing a specific instruction we cannot verify would be worse than
    saying "we don't know where".
    """
    parts = dotted_path.split(".")
    if len(parts) >= 2 and parts[0] == "channels":
        channel = parts[1]
        label = _channel_label(channel)
        hint = _CHANNEL_REVOKE_HINTS.get(channel)
        if hint:
            return f"{label}: {hint}"
        return f"{label}: revoke/rotate in the {label} developer console"
    if dotted_path.startswith("gateway."):
        return (
            "OpenClaw gateway token — rotate by redeploying the bot, or delete "
            "the field and restart the gateway if the bot is gone for good"
        )
    return "rotate at the issuing provider"


def _channel_label(channel: str) -> str:
    """Display label from ``channel_registry`` when reachable, else the id.

    Lazy admin import: this module lives in the analyzer package, which must
    never carry ``evolve_admin`` in its load-time graph. A missing registry
    just degrades the label.
    """
    try:
        from evolve_admin import channel_registry as cr  # type: ignore
    except ImportError:
        return channel
    # ``display_label`` already title-cases an unregistered id, so a channel
    # the registry has never heard of still reads like a product name.
    return cr.display_label(channel) or channel


def harvest_config_secrets(
    config: "dict[str, Any]", *, _prefix: str = "", _depth: int = 0,
) -> "list[CredentialArtifact]":
    """Every secret-shaped field in a parsed ``openclaw.json``, value-free.

    Walks the whole config by key name, so a channel block Evolve has never
    heard of is covered by the same walk as ``channels.telegram.botToken``.
    """
    out: "list[CredentialArtifact]" = []
    if _depth > _MAX_WALK_DEPTH:
        return out
    for key, value in config.items():
        dotted = f"{_prefix}{key}"
        if isinstance(value, dict):
            out.extend(
                harvest_config_secrets(
                    value, _prefix=f"{dotted}.", _depth=_depth + 1
                )
            )
            continue
        if not _is_secret_key(key):
            continue
        shape, live = _classify_value(value)
        if not shape:
            continue
        out.append(
            CredentialArtifact(
                location=f"openclaw.json::{dotted}",
                kind="config_field",
                shape=shape,
                revoke_with=_revoke_hint_for(dotted),
                live=live,
            )
        )
    return sorted(out, key=lambda a: a.location)


# ── Filesystem harvest ────────────────────────────────────────────────────


def _ssh_key_artifacts(home: Path, inv: Inventory) -> None:
    """Private-key-shaped files under ``~/.ssh``."""
    names, status = _list_dir(home / ".ssh")
    if status == "absent":
        return
    if names is None:
        inv.unread.append(f"{home}/.ssh — {status}")
        return
    pubs = {n[: -len(".pub")] for n in names if n.endswith(".pub")}
    for name in names:
        if name.endswith(".pub") or name in _SSH_NON_KEY_NAMES:
            continue
        # A file with a matching .pub sibling is definitively a keypair; a
        # bare file under ~/.ssh is reported too (an unpaired private key is
        # if anything more suspicious), with the weaker claim stated.
        paired = name in pubs
        inv.artifacts.append(
            CredentialArtifact(
                location=f"{home}/.ssh/{name}",
                kind="ssh_private_key",
                shape="private key file" + ("" if paired else " (no .pub sibling)"),
                revoke_with=(
                    "remove the matching public key from wherever it was "
                    "authorized (GitHub deploy keys, the backup remote, "
                    "another host's authorized_keys), then delete the file"
                ),
            )
        )


# Credential directories to enumerate, as
# ``(relpath, kind, revoke_hint, skip_suffixes)``. ``skip_suffixes`` exists
# because ``.openclaw/credentials/`` mixes real pairing secrets
# (``<ch>-pairing.json``) with plain allowlists (``<ch>-default-allowFrom.json``)
# — calling an allowlist a credential would send the operator chasing a
# revocation that does not exist.
_CREDENTIAL_DIRS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "credentials",
        "pairing_credential",
        "channel pairing secret — invalidated by re-pairing the channel",
        ("-allowFrom.json",),
    ),
    (
        "workspace/credentials",
        "workspace_credential",
        "app credential — rotate at the issuing provider",
        (),
    ),
    (
        "google_workspace_mcp/credentials",
        "google_oauth_credential",
        "Google OAuth token — revoke at myaccount.google.com/permissions, "
        "then delete the file",
        (),
    ),
)


def _credential_dir_artifacts(home: Path, inv: Inventory) -> None:
    """Token-bearing files under the OpenClaw credential directories."""
    oc = home / ".openclaw"
    for rel, kind, hint, skip_suffixes in _CREDENTIAL_DIRS:
        directory = oc / rel
        names, status = _list_dir(directory)
        if status == "absent":
            continue
        if names is None:
            inv.unread.append(f"{directory} — {status}")
            continue
        for name in names:
            if name.endswith(skip_suffixes) if skip_suffixes else False:
                continue
            inv.artifacts.append(
                CredentialArtifact(
                    location=str(directory / name),
                    kind=kind,
                    shape="credential file",
                    revoke_with=hint,
                )
            )


def _auth_store_artifacts(home: Path, inv: Inventory) -> None:
    """The per-agent OpenClaw auth store (LLM provider keys / OAuth grants)."""
    agent_dir = home / ".openclaw" / "agents" / "main" / "agent"
    for name, kind, hint in (
        (
            "auth-profiles.json",
            "llm_auth_profile",
            "LLM provider keys — rotate in the provider console "
            "(console.anthropic.com, platform.openai.com, …)",
        ),
        (
            "openclaw-agent.sqlite",
            "llm_auth_store",
            "OpenClaw's per-agent auth store — holds provider keys and OAuth "
            "grants; rotate in the provider console",
        ),
    ):
        path = agent_dir / name
        status = _probe(path)
        if status == "absent":
            continue
        if status == "unreadable":
            inv.unread.append(f"{path} — unreadable")
            continue
        inv.artifacts.append(
            CredentialArtifact(
                location=str(path),
                kind=kind,
                shape="credential store present",
                revoke_with=hint,
            )
        )


# ── Entry point ───────────────────────────────────────────────────────────


def collect(home: Path) -> Inventory:
    """Inventory every token-bearing artifact under one account's *home*.

    Never raises for a per-artifact read failure — those land in
    :attr:`Inventory.unread` so the caller can say "and N locations could not
    be checked" instead of implying the account is clean.
    """
    inv = Inventory()

    config, status = read_openclaw_config(home)
    if config is not None:
        inv.artifacts.extend(harvest_config_secrets(config))
    elif status != "absent":
        inv.unread.append(f"{home}/.openclaw/openclaw.json — {status}")

    _ssh_key_artifacts(home, inv)
    _credential_dir_artifacts(home, inv)
    _auth_store_artifacts(home, inv)

    inv.artifacts.sort(key=lambda a: (not a.live, a.kind, a.location))
    return inv


def summarize(inv: Inventory) -> str:
    """One-line count for a Signal title / CLI line ("3 live, 1 unreadable")."""
    live = len(inv.live_artifacts)
    parts = [f"{live} live credential{'' if live == 1 else 's'}"]
    if inv.is_blind:
        parts.append(f"{len(inv.unread)} location(s) unreadable")
    return ", ".join(parts)


def as_markdown(inv: Inventory) -> str:
    """The inventory as a Markdown fragment for the retire closure summary."""
    lines: list[str] = []
    live = inv.live_artifacts
    if live:
        for a in live:
            lines.append(f"- `{a.location}` — {a.shape}")
            lines.append(f"  - **Revoke**: {a.revoke_with}")
    else:
        lines.append("- No live credentials found in this account.")

    considered = [a for a in inv.artifacts if not a.live]
    if considered:
        lines.append("")
        lines.append("Checked and NOT a live secret on disk:")
        for a in considered:
            lines.append(f"- `{a.location}` — {a.shape}")

    if inv.is_blind:
        lines.append("")
        lines.append(
            "**Could not be read — this list may be incomplete:**"
        )
        for u in inv.unread:
            lines.append(f"- `{u}`")
    return "\n".join(lines)
