"""``evo bug`` / ``evo feature`` / ``evo intake [list|promote]`` handlers.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §6.

`evo bug` and `evo feature` capture a new intake. With `--post` they
also promote it to GitHub in one shot (requires `evolve-admin intake
configure`).

`evo intake` without args, or `evo intake list`, renders the recent
queue. `evo intake promote <id>` files a specific intake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..identity import Role
from ._shared import speak
from ...intake import envelope as _env
from ...intake import promote as _promote
from ...intake import store as _store


# ─────────────────────────────────────────────────────────────────────────────
# Capture: evo bug / evo feature
# ─────────────────────────────────────────────────────────────────────────────


def render_bug(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    return _capture(role, bot_id, args, network, kind="bug", subcommand="bug")


def render_feature(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    return _capture(role, bot_id, args, network, kind="feature", subcommand="feature")


def _capture(
    role: Role,
    bot_id: str,
    args: str,
    network: dict[str, Any],
    *,
    kind: _env.IntakeKind,
    subcommand: str,
):
    body, post, target_name = _parse_capture_args(args)
    if not body:
        return speak(
            subcommand,
            (
                f"**{subcommand.title()}**\n\n"
                f"Tell me what to capture, e.g. `evo {subcommand} when I run "
                f"X, Y happens`.\n"
                f"Add `--post` to file the issue to GitHub in one step."
            ),
            role,
        )

    shared_dir = _shared_dir(network)
    intake = _env.Intake(
        id=_store.new_intake_id(),
        kind=kind,
        body=body,
        context=_env.IntakeContext(
            primary_bot=_primary_bot(network),
            active_bot=bot_id,
            git_commit=_current_git_commit(),
            evolve_version=_current_evolve_version(),
        ),
    )

    try:
        _store.write_intake(intake, shared_dir)
    except (PermissionError, OSError) as e:
        return speak(
            subcommand,
            f"**{subcommand.title()}**\n\nCouldn't save the intake: {e}",
            role,
        )

    if not post:
        return speak(
            subcommand,
            (
                f"**Captured** — `{intake.id}` ({kind})\n\n"
                f"> {_preview(body)}\n\n"
                f"Review with `evo intake list`, file with "
                f"`evo intake promote {intake.id}`."
            ),
            role,
        )

    # --post fast path: capture + promote. On failure, the captured intake
    # remains in open/ and can be retried — surface the id so the user
    # knows it isn't lost.
    return _do_promote(
        role,
        network,
        shared_dir,
        intake.id,
        include_transcript=False,
        subcommand=subcommand,
        post_failed_hint_id=intake.id,
        target_name=target_name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Review / promote: evo intake [list|promote]
# ─────────────────────────────────────────────────────────────────────────────


def render(*, role: Role, bot_id: str, args: str, network: dict[str, Any]):
    parts = (args or "").strip().split(maxsplit=1)
    head = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if head in ("", "list"):
        kind_filter = rest.strip().lower() or None
        if kind_filter and kind_filter not in ("bug", "feature", "question"):
            return speak(
                "intake",
                f"**Intake**\n\nUnknown kind `{kind_filter}` — use bug | feature | question.",
                role,
            )
        return _render_list(role, _shared_dir(network), kind_filter)

    if head == "promote":
        intake_id, target_name = _parse_promote_args(rest)
        if not intake_id:
            return speak(
                "intake",
                "**Intake**\n\nUsage: `evo intake promote <id> [--to <target>]` — "
                "copy the id from `evo intake list`. Optional `--to` picks a non-default "
                "configured target (e.g. `--to openclaw`).",
                role,
            )
        return _do_promote(
            role,
            network,
            _shared_dir(network),
            intake_id,
            include_transcript=False,
            subcommand="intake",
            target_name=target_name,
        )

    return speak(
        "intake",
        (
            "**Intake**\n\n"
            "Forms: `evo intake`, `evo intake list [bug|feature]`, "
            "`evo intake promote <id> [--to <target>]`."
        ),
        role,
    )


def _render_list(role: Role, shared_dir: Path, kind_filter: str | None):
    rows = list(
        _store.iter_intakes(
            shared_dir,
            subdirs=("open", "triaged"),
            kind=kind_filter,
        )
    )
    if not rows:
        suffix = f" ({kind_filter})" if kind_filter else ""
        return speak(
            "intake",
            f"**Intake{suffix}**\n\nNothing in the queue. 🟢",
            role,
        )

    rows.sort(key=lambda ix: ix.created_at, reverse=True)
    lines = [f"**Intake — {len(rows)} open**", ""]
    for ix in rows[:10]:
        lines.append(
            f"• `{ix.id}` ({ix.kind}) — {_preview(ix.body, n=60)}"
        )
    if len(rows) > 10:
        lines.append(f"…and {len(rows) - 10} more.")
    lines.append("")
    lines.append("File one: `evo intake promote <id>`.")
    return speak("intake", "\n".join(lines), role)


def _do_promote(
    role: Role,
    network: dict[str, Any],
    shared_dir: Path,
    intake_id: str,
    *,
    include_transcript: bool,
    subcommand: str,
    post_failed_hint_id: str | None = None,
    target_name: str | None = None,
):
    """Promote intake to GitHub.

    ``post_failed_hint_id`` — when called from the ``--post`` fast path,
    the caller passes the just-captured id so failure messages can tell
    the user "still saved as <id>, retry with `evo intake promote <id>`"
    rather than implying the bug report was lost.

    ``target_name`` — named intake target (e.g. "evolve" or "openclaw");
    None means use the configured default target.
    """
    located = _store.find_intake(shared_dir, intake_id)
    if located is None:
        return speak(subcommand, f"**Intake**\n\nNo intake with id `{intake_id}`.", role)
    intake, _, _ = located

    def _fail(msg: str):
        if post_failed_hint_id:
            msg = (
                f"{msg}\n\n"
                f"_(Captured as `{post_failed_hint_id}` — retry with "
                f"`evo intake promote {post_failed_hint_id}`.)_"
            )
        return speak(subcommand, f"**Intake**\n\n{msg}", role)

    cfg = _promote.PromotionConfig.from_network(network)
    if cfg is None:
        return _fail(
            "GitHub target isn't set up yet. Run "
            "`evolve-admin intake configure --owner <org> --repo <repo>` "
            "and store a token in the keystore."
        )

    try:
        target = cfg.resolve(target_name)
    except _promote.PromotionError as e:
        return _fail(str(e))

    # Lazy import — KeystoreManager pulls in optional deps.
    from ...keystore import KeystoreManager
    token = KeystoreManager(shared_dir).get_value(target.token_slot)
    if not token:
        return _fail(
            f"No token in keystore slot `{target.token_slot}`. Set it with "
            f"`evolve-admin keys set {target.token_slot}`."
        )

    try:
        updated = _promote.promote(
            intake,
            network=network,
            shared_dir=shared_dir,
            token=token,
            include_transcript=include_transcript,
            promoted_by="evo",
            target_name=target.name,
        )
    except _promote.PromotionError as e:
        return _fail(f"Couldn't file: {e}")

    return speak(
        subcommand,
        (
            f"**Filed** — `{updated.id}`\n\n"
            f"{updated.promotion.github_issue_url or '(no url)'}"
        ),
        role,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Parsing + helpers
# ─────────────────────────────────────────────────────────────────────────────


def _parse_capture_args(args: str) -> tuple[str, bool, str | None]:
    """Pull ``--post`` and ``--to <target>`` flags off the body.

    Tolerates flags at either end of the body. Returns
    ``(body, post, target_name)``; target_name is None when ``--to`` was
    not given.

    A bare ``--post`` (with nothing left after stripping flags) is
    treated as missing body so the user sees the usage hint.
    """
    raw = (args or "").strip()
    if not raw:
        return "", False, None
    tokens = raw.split()
    post = False
    target_name: str | None = None

    # Repeatedly strip recognized flags from either end. Order-tolerant.
    changed = True
    while changed and tokens:
        changed = False
        if tokens[0] == "--post":
            post = True
            tokens = tokens[1:]
            changed = True
            continue
        if tokens[-1] == "--post":
            post = True
            tokens = tokens[:-1]
            changed = True
            continue
        # --to <name>: consumes two tokens
        if len(tokens) >= 2 and tokens[0] == "--to":
            target_name = tokens[1]
            tokens = tokens[2:]
            changed = True
            continue
        if len(tokens) >= 2 and tokens[-2] == "--to":
            target_name = tokens[-1]
            tokens = tokens[:-2]
            changed = True
            continue

    body = " ".join(tokens).strip()
    return body, post, target_name


def _parse_promote_args(args: str) -> tuple[str, str | None]:
    """Parse ``<intake_id> [--to <target>]``.

    Returns ``(intake_id, target_name)``; ``intake_id`` is "" if the
    caller forgot it. ``target_name`` is None when ``--to`` is omitted.
    Tolerates the flag and id appearing in either order.
    """
    raw = (args or "").strip()
    if not raw:
        return "", None
    tokens = raw.split()
    target_name: str | None = None

    # Strip --to <name> from anywhere; whatever's left is the intake id.
    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "--to" and i + 1 < len(tokens):
            target_name = tokens[i + 1]
            i += 2
            continue
        cleaned.append(tokens[i])
        i += 1

    intake_id = cleaned[0] if cleaned else ""
    return intake_id, target_name


def _preview(text: str, n: int = 80) -> str:
    flat = " ".join(text.split())
    if len(flat) <= n:
        return flat
    return flat[: n - 1].rstrip() + "…"


def _shared_dir(network: dict[str, Any]) -> Path:
    return Path(network.get("sharedDir", "/Users/Shared/evolve"))


def _primary_bot(network: dict[str, Any]) -> str | None:
    pb = network.get("primary")
    if isinstance(pb, str) and pb.strip():
        return pb
    return None


def _current_git_commit() -> str | None:
    """Best-effort: short SHA of the deploy-checkout HEAD.

    Returns None if we're not in a git working tree (e.g. dist install).
    Used as a context hint on captured intakes, not load-bearing.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _current_evolve_version() -> str | None:
    """Best-effort: EVOLVE_VERSION constant from deploy.py.

    Lazy-imported so the handler module load doesn't pull deploy.py in.
    Returns None on any error so a partially-broken deploy doesn't
    poison the intake capture path.
    """
    try:
        from ...deploy import EVOLVE_VERSION
        return str(EVOLVE_VERSION) if EVOLVE_VERSION else None
    except Exception:  # noqa: BLE001
        return None
