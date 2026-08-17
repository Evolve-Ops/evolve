"""CLI device-scope invariant for each bot's own OpenClaw CLI device.

Incident (2026-06-11, P0 delivery-migration rollout — spec
docs/spec-gallery-delivery-convention-2026-06-11.md §6 step 0): the OC
2026.6 upgrade narrowed every bot's own CLI device to
``scopes: ["operator.read"]`` in ``~/.openclaw/devices/paired.json``.
``openclaw message send`` and every CLI-via-gateway surface (including
defer_runner's ``openclaw agent --deliver`` fires) died with "scope
upgrade pending approval" — and the approval flow cannot self-serve,
because each CLI attempt files a fresh pairing request that supersedes
the one the operator is trying to approve.

This module makes the manual repair durable. The invariant is anchored
on the bot's *current* CLI identity (``~/.openclaw/identity/device.json``):

- **Repair**: the paired entry whose deviceId matches the current
  identity must carry ``operator.read``, ``operator.write`` and
  ``operator.pairing`` in all three scope lists — ``scopes``,
  ``approvedScopes``, and ``tokens.operator.scopes``. Narrowed lists
  are widened in place (extra scopes are preserved); the gateway is
  then kickstarted so it serves the repaired baseline. Only that one
  entry is touched: other ``clientId == "cli"`` devices (an operator's
  laptop CLI paired against this gateway, dead entries from old
  identity rotations) may be deliberately narrow and are never
  auto-escalated.

- **Day-1 seed**: a fresh bot has no paired entry for its identity (or
  no identity at all), so its first CLI use would file a pairing
  request and die. Rather than scripting one CLI invocation plus an
  approval dance (unreliable: the superseding-request bug; and
  bot-context ``openclaw`` subprocesses have a history of wedging
  deploys — see the nightly-doctor migration note in deploy.py),
  deploy pre-seeds a fully approved CLI device entry. When the
  identity file is missing a fresh Ed25519 identity is generated for
  it — the OC CLI validates the file on startup (fingerprint + keypair
  self-check) and keeps any valid identity it finds. The seeded entry
  carries no ``tokens`` block: the gateway's ``ensureDeviceToken``
  mints a token within the approved baseline on first connect
  (verified against the OC 2026.6 dist — ``cloneDeviceTokens``
  tolerates an absent ``tokens`` key).

Fail-safe rules (each violated direction caused a real review finding):

- A malformed/unreadable paired.json is never rewritten — other paired
  devices (operator web-UI sessions, …) live in it.
- A present-but-invalid identity file is reported as drift but never
  regenerated or repaired around: it may be a future OC identity
  format, and auto-rewriting v1 would fight the CLI on every pass.
  The deliberate cost: if a future OC upgrade changes the identity
  format AND narrows scopes, this module reports loudly instead of
  repairing — by then the wire-format knowledge below needs re-pinning
  anyway.
- **Revocation contract**: deleting a bot's CLI paired entry alone is
  NOT durable on an Evolve pod — the next deploy re-seeds it (that is
  the point of the invariant). To revoke a compromised bot CLI key,
  delete ``identity/device.json`` AND the paired entry: the seeder
  then mints a *fresh* keypair, leaving the compromised key dead.
- Writes are an unlocked read-modify-write against a file the live
  gateway also rewrites (pairing approvals, token mints). The window
  between read and ``sudo cp`` is milliseconds and repairs fire only
  on actual drift, but a concurrent gateway write can lose; that
  device re-pairs. The bail-on-unparseable rule above removes the
  catastrophic variant (clobbering the whole device list).

Upstream-policy note: today the 2026.6 narrowing is treated as a bug
because OC offers no working re-grant path. If a future OC release
ships a sanctioned scope-grant command for local CLI devices, prefer
adopting it over this direct-file repair (see
memory: dont-reimplement-upstream / the openclaw releases page).

Wire format facts (mirrors OC's ``src/infra/device-identity.ts``):

- ``deviceId`` = sha256 hex of the raw 32-byte Ed25519 public key.
- paired.json ``publicKey`` = base64url (no padding) of the raw key.
- ``identity/device.json`` stores the same key PEM-encoded (SPKI /
  PKCS8); the raw key is the DER minus the fixed 12-byte prefix.

File access follows the CLAUDE.md contract: reads are direct (evolve
holds an inherited read ACL on ``.openclaw/``) with a ``sudo cat``
fallback; writes go through /tmp staging + ``sudo cp`` + chown/chmod
(the files are bot-owned mode 600). The sudoers grants live in
``_render_evolve_sudoers`` §5c — the write grants are exercised by the
admin-daemon (evolve-user) deploy/provision paths; root CLI deploys
don't need them. NOTE for existing pods: the grants land only after
``sudo evolve-admin refresh-sudoers``.

Callers:
- ``deploy._check_cli_device_scopes`` — per-bot check in
  ``ensure_pod_perms`` (every deploy applies; the hourly
  pod_perms_drift_monitor turns re-narrowing between deploys into a
  Signal).
- ``ocadmin.oc_upgrade`` — post-install repair pass, because the OC
  upgrade path is the suspected narrowing mechanism and the next
  upgrade may do it again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeGuard

from evolve_config import user_home

from .runtime import get_scheduler

_log = logging.getLogger("evolve.oc_cli_device")

# The full scope set the bot's own CLI device needs. operator.read alone
# (the OC 2026.6 narrowed state) breaks `openclaw message send` and every
# defer/deliver fire; operator.pairing is required so the CLI can manage
# its own re-pairing without operator surgery.
CLI_DEVICE_SCOPES: tuple[str, ...] = (
    "operator.read", "operator.write", "operator.pairing",
)
CLI_CLIENT_ID = "cli"
OPERATOR_ROLE = "operator"

# Fixed DER prefix for Ed25519 SPKI (same constant OC uses).
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")

PAIRED_REL = Path("devices/paired.json")
IDENTITY_REL = Path("identity/device.json")


# ── subprocess seam ──────────────────────────────────────────────────────────
# Tests inject a fake via set_runner() instead of patching this module's
# subprocess attribute — patch-by-module-path fakes silently stop
# intercepting when code moves (the #2629 lesson). A guard test pins
# every spawn to this seam.

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
_runner: Runner = subprocess.run


def set_runner(fn: Runner) -> Runner:
    """Swap the sudo-subprocess runner (test seam). Returns the previous one."""
    global _runner
    prev = _runner
    _runner = fn
    return prev


def _run(argv: list[str], timeout: int = 10) -> "subprocess.CompletedProcess[str]":
    return _runner(argv, capture_output=True, text=True, timeout=timeout)


def _run_sudo_steps(argvs: "tuple[list[str], ...]") -> "tuple[bool, str]":
    """Run privileged steps in order; stop at the first failure."""
    for argv in argvs:
        r = _run(argv)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            return False, f"{' '.join(argv[:2])} failed: {err}"
    return True, ""


def _commands() -> dict[str, str]:
    """Platform command table — same single source the sudoers renderer uses,
    so the argv here can never drift from the grants."""
    from platform_profile import get_profile
    return get_profile().commands


# ── pure helpers (no IO) ─────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def public_key_raw_from_pem(pem: str) -> bytes:
    """Extract the raw 32-byte Ed25519 key from an SPKI PEM.

    Raises ValueError on anything that isn't an Ed25519 SPKI blob —
    callers treat that as "identity file invalid".
    """
    body = "".join(
        line.strip() for line in pem.splitlines()
        if line.strip() and not line.startswith("-----")
    )
    der = base64.b64decode(body, validate=True)
    if len(der) != len(_ED25519_SPKI_PREFIX) + 32 or not der.startswith(_ED25519_SPKI_PREFIX):
        raise ValueError("not an Ed25519 SPKI public key")
    return der[len(_ED25519_SPKI_PREFIX):]


def fingerprint_public_key(pem: str) -> str:
    """OC's deviceId derivation: sha256 hex of the raw public key."""
    return hashlib.sha256(public_key_raw_from_pem(pem)).hexdigest()


def _widen_scopes(value: Any) -> "tuple[list[str], bool]":
    """Return (widened list, changed). Preserves existing order and any
    extra scopes; a non-list value is replaced by the canonical set."""
    if not isinstance(value, list):
        return list(CLI_DEVICE_SCOPES), True
    current = [s for s in value if isinstance(s, str)]
    missing = [s for s in CLI_DEVICE_SCOPES if s not in current]
    if not missing and len(current) == len(value):
        return value, False
    return current + missing, True


def _is_own_cli_entry(entry: Any) -> "TypeGuard[dict[str, Any]]":
    """True when the entry is plausibly the bot's own CLI device record."""
    return (
        isinstance(entry, dict)
        and entry.get("clientId") == CLI_CLIENT_ID
        and entry.get("role", OPERATOR_ROLE) == OPERATOR_ROLE
    )


def repair_cli_device(paired: dict, device_id: str) -> "tuple[dict, list[str]]":
    """Widen the scope lists of the entry for ``device_id``. Pure —
    returns (new_dict, notes); empty notes = already satisfied.

    Deliberately targeted: ONLY the bot's own current CLI device is
    widened. Other clientId=="cli" entries (operator laptop CLIs, dead
    identity rotations) may be narrow on purpose — auto-escalating
    them would grant write+pairing to devices an operator restricted.
    """
    entry = paired.get(device_id)
    if not _is_own_cli_entry(entry):
        return paired, []
    notes: list[str] = []
    new_entry = dict(entry)
    short = str(device_id)[:12]
    for field_name in ("scopes", "approvedScopes"):
        widened, changed = _widen_scopes(new_entry.get(field_name))
        if changed:
            new_entry[field_name] = widened
            notes.append(f"{short}.{field_name}")
    tokens = new_entry.get("tokens")
    if isinstance(tokens, dict):
        op_token = tokens.get(OPERATOR_ROLE)
        if isinstance(op_token, dict):
            widened, changed = _widen_scopes(op_token.get("scopes"))
            if changed:
                new_tokens = dict(tokens)
                new_tokens[OPERATOR_ROLE] = {**op_token, "scopes": widened}
                new_entry["tokens"] = new_tokens
                notes.append(f"{short}.tokens.operator.scopes")
    if not notes:
        return paired, []
    out = dict(paired)
    out[device_id] = new_entry
    return out, notes


def build_seed_entry(device_id: str, public_key_b64url: str,
                     *, platform: str, now_ms: int) -> dict:
    """A fully-approved paired-device entry for the bot's own CLI.

    No ``tokens`` block on purpose — the gateway mints one within the
    ``approvedScopes`` baseline on the CLI's first connect.
    """
    return {
        "deviceId": device_id,
        "publicKey": public_key_b64url,
        "platform": platform,
        "clientId": CLI_CLIENT_ID,
        "clientMode": "cli",
        "role": OPERATOR_ROLE,
        "roles": [OPERATOR_ROLE],
        "scopes": list(CLI_DEVICE_SCOPES),
        "approvedScopes": list(CLI_DEVICE_SCOPES),
        "createdAtMs": now_ms,
        "approvedAtMs": now_ms,
    }


def _identity_shape_ok(identity: Any) -> bool:
    """Cheap structural validation of identity/device.json — public
    material only (version 1, PEM fields present, deviceId matches the
    public-key fingerprint). Used on the read-only check path, which
    runs hourly per bot: no cryptography import, no private-key use."""
    if not isinstance(identity, dict) or identity.get("version") != 1:
        return False
    pub = identity.get("publicKeyPem")
    priv = identity.get("privateKeyPem")
    device_id = identity.get("deviceId")
    if not (isinstance(pub, str) and isinstance(priv, str) and isinstance(device_id, str)):
        return False
    try:
        return fingerprint_public_key(pub) == device_id
    except ValueError:  # covers binascii.Error from b64decode
        return False


def _identity_keypair_ok(identity: dict) -> bool:
    """Full sign/verify self-check (mirrors the OC CLI's own acceptance
    test). Only run immediately before seeding an entry — never on the
    hourly check path. The cryptography import sits OUTSIDE the except:
    a broken interpreter (missing dep — the #2690 class) must surface
    as an ImportError in the apply error log, not masquerade as an
    'invalid identity' diagnosis."""
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key, load_pem_public_key,
    )
    try:
        private_key = load_pem_private_key(
            identity["privateKeyPem"].encode(), password=None)
        public_key = load_pem_public_key(identity["publicKeyPem"].encode())
        payload = b"evolve-cli-device-self-check"
        public_key.verify(private_key.sign(payload), payload)  # type: ignore[union-attr, call-arg]
        return True
    except Exception:
        return False


def generate_identity(now_ms: int) -> dict:
    """Fresh Ed25519 CLI identity in OC's identity/device.json shape."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return {
        "version": 1,
        "deviceId": fingerprint_public_key(pub_pem),
        "publicKeyPem": pub_pem,
        "privateKeyPem": priv_pem,
        "createdAtMs": now_ms,
    }


# ── file IO (ACL-direct reads, /tmp-staged sudo writes) ──────────────────────

def _read_json(path: Path) -> "tuple[Any, str]":
    """Read+parse a bot-owned JSON file. Returns (value, status) with
    status ∈ {ok, missing, malformed, unreadable}."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        r = _run(["sudo", _commands()["cat"], str(path)])
        if r.returncode != 0:
            combined = f"{r.stderr or ''}{r.stdout or ''}"
            if "No such file" in combined:
                return None, "missing"
            return None, "unreadable"
        text = r.stdout
    except OSError:
        return None, "unreadable"
    try:
        return json.loads(text), "ok"
    except (json.JSONDecodeError, ValueError):
        return None, "malformed"


def _install_json(path: Path, payload: dict, bot_user: str) -> "tuple[bool, str]":
    """/tmp staging + sudo cp + chown bot:staff + chmod 600.

    One write path for every caller context (root deploy, evolve-user
    daemon): direct writes as root would leave a root-owned file the
    gateway (running as the bot) can't rewrite. The staging prefix must
    stay ``evolve-device-`` — the sudoers cp grant is pinned to it.
    The staged file keeps mkstemp's 0600: the payload can be an Ed25519
    PRIVATE key (identity seed) or operator tokens, and root cp reads
    0600 fine — never relax it.
    """
    c = _commands()
    fd, tmp = tempfile.mkstemp(dir="/tmp", prefix="evolve-device-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
        return _run_sudo_steps((
            ["sudo", c["cp"], tmp, str(path)],
            ["sudo", c["chown"], f"{bot_user}:staff", str(path)],
            ["sudo", c["chmod"], "600", str(path)],
        ))
    finally:
        Path(tmp).unlink(missing_ok=True)


def _ensure_dir(path: Path, bot_user: str) -> "tuple[bool, str]":
    """mkdir -p + chown bot:staff + chmod 700 — only called when absent."""
    c = _commands()
    return _run_sudo_steps((
        ["sudo", c["mkdir"], "-p", str(path)],
        ["sudo", c["chown"], f"{bot_user}:staff", str(path)],
        ["sudo", c["chmod"], "700", str(path)],
    ))


# ── check / ensure entrypoints ───────────────────────────────────────────────

def _oc_dir(bot_user: str) -> Path:
    # evolve_config.user_home is pwd-first with a platform-profile
    # fallback — NOT a hardcoded /Users/: Linux pods keep homes under
    # /home and this invariant must fire there too (the §5c sudoers
    # grants render for both platforms).
    return user_home(bot_user) / ".openclaw"


# Overridable resolver — tests point this at a tmp tree. Production
# callers never touch it. (Kept as a seam instead of importing deploy's
# _user_home: this module must stay importable from ocadmin and the
# analyzer-side monitors without dragging deploy's 10k lines in;
# evolve_config is analyzer-side and already a dependency.)
_oc_dir_resolver: Callable[[str], Path] = _oc_dir


def set_oc_dir_resolver(fn: "Callable[[str], Path]") -> "Callable[[str], Path]":
    global _oc_dir_resolver
    prev = _oc_dir_resolver
    _oc_dir_resolver = fn
    return prev


@dataclass
class CliDeviceCheck:
    """Read-only assessment of one bot's CLI device pairing state."""
    ok: bool
    detail: str
    needs_repair: bool = False     # current identity's entry is narrowed
    needs_seed: bool = False       # current identity has no paired entry

    @property
    def fixable(self) -> bool:
        """ensure_cli_device_scopes() can repair this. Derived, so the
        apply gate can never disagree with the flags."""
        return self.needs_repair or self.needs_seed


@dataclass
class EnsureOutcome:
    ok: bool
    changed: bool
    detail: str


def check_cli_device_scopes(bot_user: str) -> CliDeviceCheck:
    """Assess without mutating. Safe for the hourly drift monitor (evolve
    user, check-only): reads ride the inherited ACL / sudo-cat fallback,
    and no private-key cryptography runs on this path.

    Anchored on the current CLI identity:

    - identity valid + entry present → widen-check that one entry
    - identity valid + entry absent  → seed drift
    - identity missing               → generate + seed drift
    - identity present-but-invalid   → loud drift, NOT fixable: it may
      be a future OC identity format; regenerating would fight the CLI
      on every pass, and we cannot tell which paired entry is the
      bot's own, so no repair target exists either.
    """
    oc = _oc_dir_resolver(bot_user)
    if not oc.exists():
        return CliDeviceCheck(ok=True, detail="(bot not yet bootstrapped — skipping)")

    paired, status = _read_json(oc / PAIRED_REL)
    if status == "unreadable":
        return CliDeviceCheck(ok=False, detail="paired.json unreadable")
    if status == "malformed" or (status == "ok" and not isinstance(paired, dict)):
        # Fail safe: never rewrite a file we can't parse — other paired
        # devices (operator web UI sessions, …) live in it too. The
        # gateway itself coerces malformed state to empty, so this is an
        # operator-attention state, not a repair target.
        return CliDeviceCheck(ok=False, detail="paired.json malformed — refusing to rewrite")
    if status == "missing":
        paired = {}

    identity, id_status = _read_json(oc / IDENTITY_REL)
    if id_status == "missing":
        return CliDeviceCheck(
            ok=False, detail="no CLI identity yet (generate + seed)",
            needs_seed=True,
        )
    if id_status != "ok" or not _identity_shape_ok(identity):
        return CliDeviceCheck(
            ok=False,
            detail=(
                "identity/device.json unreadable" if id_status == "unreadable"
                else "identity/device.json fails the v1 shape check — not "
                     "auto-repaired (possible OC identity-format change; "
                     "re-pin oc_cli_device wire-format knowledge)"
            ),
        )

    device_id = identity["deviceId"]
    entry = paired.get(device_id)
    if entry is None:
        return CliDeviceCheck(
            ok=False,
            detail=f"current CLI identity {device_id[:12]} has no paired "
                   f"entry (seed from existing identity)",
            needs_seed=True,
        )
    if not _is_own_cli_entry(entry):
        return CliDeviceCheck(
            ok=False,
            detail=f"paired entry for current CLI identity {device_id[:12]} "
                   f"is not a cli/operator record "
                   f"(clientId={entry.get('clientId')!r}, role={entry.get('role')!r}) "
                   f"— operator attention needed",
        )
    _, notes = repair_cli_device(paired, device_id)
    if notes:
        return CliDeviceCheck(
            ok=False,
            detail=f"CLI device scopes narrowed: {', '.join(notes)}",
            needs_repair=True,
        )
    return CliDeviceCheck(
        ok=True,
        detail=f"CLI device {device_id[:12]} carries {', '.join(CLI_DEVICE_SCOPES)}",
    )


def ensure_cli_device_scopes(
    bot_id: str,
    bot_user: "str | None" = None,
    *,
    restart: bool = True,
) -> EnsureOutcome:
    """Repair/seed the bot's CLI device entry; kickstart the gateway on change.

    Idempotent: a bot already satisfying the invariant returns
    ``changed=False`` and never touches a file or restarts anything.
    ``restart=False`` is for callers that restart gateways themselves
    right after (the OC upgrade path).
    """
    bot_user = bot_user or bot_id
    check = check_cli_device_scopes(bot_user)
    if check.ok:
        return EnsureOutcome(ok=True, changed=False, detail=check.detail)
    if not check.fixable:
        return EnsureOutcome(ok=False, changed=False, detail=check.detail)

    oc = _oc_dir_resolver(bot_user)
    paired_path = oc / PAIRED_REL
    now_ms = int(time.time() * 1000)
    actions: list[str] = []

    # Re-read at apply time (smallest staleness window vs the live
    # gateway) — and bail on anything unparseable: writing through a
    # malformed/unreadable re-read would clobber every other paired
    # device with an empty dict.
    paired, status = _read_json(paired_path)
    if status == "missing":
        paired = {}
    elif status != "ok" or not isinstance(paired, dict):
        return EnsureOutcome(
            ok=False, changed=False,
            detail=f"paired.json {status} on re-read — refusing to rewrite",
        )

    if check.needs_repair:
        identity, id_status = _read_json(oc / IDENTITY_REL)
        if not (id_status == "ok" and _identity_shape_ok(identity)):
            return EnsureOutcome(
                ok=False, changed=False,
                detail="identity changed under repair — re-run deploy",
            )
        paired, notes = repair_cli_device(paired, identity["deviceId"])
        if not notes:
            # Raced: the gateway (or a parallel deploy) already widened it.
            return EnsureOutcome(ok=True, changed=False,
                                 detail="already repaired (concurrent fix)")
        actions.append(f"widened {', '.join(notes)}")

    if check.needs_seed:
        identity, id_status = _read_json(oc / IDENTITY_REL)
        if id_status == "missing":
            # Day-1: generate the identity the CLI will adopt (it keeps
            # any valid v1 identity it finds on startup). Keypair
            # self-check is implicit — we minted it.
            identity = generate_identity(now_ms)
            identity_dir = oc / IDENTITY_REL.parent
            if not identity_dir.exists():
                ok, err = _ensure_dir(identity_dir, bot_user)
                if not ok:
                    return EnsureOutcome(ok=False, changed=False, detail=err)
            ok, err = _install_json(oc / IDENTITY_REL, identity, bot_user)
            if not ok:
                return EnsureOutcome(ok=False, changed=False, detail=err)
            actions.append("generated CLI identity")
        elif not (id_status == "ok" and _identity_shape_ok(identity)
                  and _identity_keypair_ok(identity)):
            # Shape drift since the check (race), or a keypair that fails
            # the sign/verify self-check the CLI itself applies — seeding
            # an entry for a key the CLI will discard creates dead weight.
            return EnsureOutcome(
                ok=False, changed=False,
                detail="identity not seedable (failed v1 self-check) — not auto-repaired",
            )
        entry = build_seed_entry(
            identity["deviceId"],
            _b64url(public_key_raw_from_pem(identity["publicKeyPem"])),
            platform="darwin" if sys.platform == "darwin" else "linux",
            now_ms=now_ms,
        )
        paired[identity["deviceId"]] = entry
        actions.append(f"seeded approved CLI device {identity['deviceId'][:12]}")
        devices_dir = paired_path.parent
        if not devices_dir.exists():
            ok, err = _ensure_dir(devices_dir, bot_user)
            if not ok:
                return EnsureOutcome(ok=False, changed=False, detail=err)

    ok, err = _install_json(paired_path, paired, bot_user)
    if not ok:
        return EnsureOutcome(ok=False, changed=False, detail=err)

    if restart:
        # The running gateway re-reads pairing state per request, but the
        # 2026-06-11 field repair needed a kickstart before the widened
        # scopes took effect (connection-level auth caches) — keep it.
        # Failure is non-fatal: a day-1 bot may not have a gateway yet.
        label = f"ai.openclaw.{bot_id}-gateway"
        try:
            restarted, restart_out = get_scheduler().restart(label)
            actions.append(
                "gateway kickstarted" if restarted
                else f"gateway restart skipped ({restart_out.strip() or 'not running'})"
            )
        except Exception as e:
            actions.append(f"gateway restart skipped ({e})")
    else:
        actions.append("gateway restart deferred to caller")

    detail = "; ".join(actions)
    _log.info("ensure_cli_device_scopes(%s): %s", bot_id, detail)
    return EnsureOutcome(ok=True, changed=True, detail=detail)
