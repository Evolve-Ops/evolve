"""Promote an intake to a GitHub issue.

Spec: docs/spec-primary-bot-interface-2026-05-14.md §6.4, extended by
docs/spec-issue-inbox-2026-05-22.md (multi-target support).

The keystore slot (default ``github_intake``, but per-target customizable)
holds a PAT (fine-grained PAT preferred). Configured targets live in
``network.json`` under ``intake.github``. We POST to
``https://api.github.com/repos/<owner>/<repo>/issues`` directly using
``urllib.request`` — no new SDK dependency, and matching the existing
``anthropic_admin`` transport pattern (injectable for tests).

Multi-target schema (v2):

.. code-block:: json

    {
      "intake": {
        "github": {
          "default": "evolve",
          "targets": {
            "evolve":   {"owner": "evolve-ops", "repo": "evolve",
                         "labels": {"bug": ["bug"]},
                         "token_slot": "github_intake"},
            "openclaw": {"owner": "openclaw", "repo": "openclaw",
                         "labels": {},
                         "token_slot": "github_intake_openclaw"}
          }
        }
      }
    }

Legacy single-target schema (v1) is still parsed — ``owner``/``repo`` at
the top of ``intake.github`` becomes a single target named ``default``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .envelope import Intake
from . import redact, store


GITHUB_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_S = 20

# Transport signature: (method, url, headers, body) -> (status_code, response_dict)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict]]


class PromotionError(RuntimeError):
    """Raised when promotion cannot proceed (config, auth, or HTTP failure)."""


# ─── Targets ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromotionTarget:
    """One GitHub repo + label policy + token-slot triple.

    A pod may configure several of these — typically one for the Evolve
    repo and one for OpenClaw — and pick which to use at promote time.
    """

    name: str
    owner: str
    repo: str
    labels_by_kind: dict[str, list[str]] = field(default_factory=dict)
    token_slot: str = "github_intake"


@dataclass(frozen=True)
class PromotionConfig:
    """Resolved GitHub-intake config — may include multiple targets.

    Use :meth:`resolve` to pick a specific target by name, or fall through
    to the configured default when ``name`` is None. Use
    :attr:`target_names` to enumerate available choices in error messages
    and CLI completion.
    """

    targets: tuple[PromotionTarget, ...]
    default_target_name: str

    def resolve(self, name: str | None = None) -> PromotionTarget:
        """Return the named target, or the default if ``name`` is None.

        Raises :class:`PromotionError` when the name doesn't match any
        configured target — error message lists the valid names so the
        operator knows what to retry with.
        """
        target_name = (name or self.default_target_name).strip()
        for t in self.targets:
            if t.name == target_name:
                return t
        valid = ", ".join(t.name for t in self.targets) or "(none)"
        raise PromotionError(
            f"unknown intake target {target_name!r}; configured targets: {valid}"
        )

    @property
    def target_names(self) -> list[str]:
        return [t.name for t in self.targets]

    @classmethod
    def from_network(cls, network: dict[str, Any]) -> "PromotionConfig | None":
        """Parse ``network.intake.github``; return None if no usable target.

        Accepts both the multi-target (v2) and legacy single-target (v1)
        schemas — see module docstring.
        """
        block = ((network.get("intake") or {}).get("github")) or {}
        if not isinstance(block, dict) or not block:
            return None

        # v2: multi-target shape.
        raw_targets = block.get("targets")
        if isinstance(raw_targets, dict) and raw_targets:
            parsed = cls._parse_targets(raw_targets)
            if not parsed:
                return None
            default_name = str(block.get("default") or "").strip()
            if not default_name or default_name not in {t.name for t in parsed}:
                # Operator omitted "default" or pointed it at a name that
                # doesn't exist. Falling through to the first declared
                # target is friendlier than refusing to parse.
                default_name = parsed[0].name
            return cls(targets=tuple(parsed), default_target_name=default_name)

        # v1: legacy single-target shape. owner/repo at top level.
        owner = (block.get("owner") or "").strip()
        repo = (block.get("repo") or "").strip()
        if not owner or not repo:
            return None
        labels = _parse_labels(block.get("labels"))
        token_slot = str(block.get("token_slot") or "github_intake")
        single = PromotionTarget(
            name="default",
            owner=owner,
            repo=repo,
            labels_by_kind=labels,
            token_slot=token_slot,
        )
        return cls(targets=(single,), default_target_name="default")

    @staticmethod
    def _parse_targets(raw: dict[str, Any]) -> list[PromotionTarget]:
        """Parse a v2 ``targets`` dict, skipping any malformed entries."""
        parsed: list[PromotionTarget] = []
        for name, entry in raw.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(entry, dict):
                continue
            owner = (entry.get("owner") or "").strip()
            repo = (entry.get("repo") or "").strip()
            if not owner or not repo:
                continue
            parsed.append(PromotionTarget(
                name=name.strip(),
                owner=owner,
                repo=repo,
                labels_by_kind=_parse_labels(entry.get("labels")),
                token_slot=str(entry.get("token_slot") or "github_intake"),
            ))
        return parsed


def _parse_labels(raw: Any) -> dict[str, list[str]]:
    """Normalize a labels-by-kind mapping; tolerate malformed entries."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v]
    return out


# ─── Transport ──────────────────────────────────────────────────────────────


def _default_transport(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
            status = resp.getcode() or 0
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = ""
    except urllib.error.URLError as e:
        return 0, {"error": f"network: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"transport: {type(e).__name__}: {e}"}
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, {"error": "non-json response", "body": raw[:500]}


# ─── Promote ────────────────────────────────────────────────────────────────


def promote(
    intake: Intake,
    *,
    network: dict[str, Any],
    shared_dir: Path,
    token: str | None,
    include_transcript: bool = False,
    promoted_by: str | None = None,
    target_name: str | None = None,
    transport: Transport | None = None,
) -> Intake:
    """File ``intake`` as a GitHub issue, persist the result, return the
    updated envelope.

    ``target_name`` picks which configured target to file against. When
    None, the configured default target is used. The resolved target's
    ``token_slot`` is independent of the caller's ``token`` argument —
    the caller is responsible for fetching the matching token from the
    keystore *before* calling, since this layer doesn't reach into the
    keystore itself.

    On success: transitions the intake to ``filed`` and writes
    ``promotion.{github_issue_url, github_issue_number, promoted_at,
    promoted_by, body_sent, redactions_applied}``.

    Raises :class:`PromotionError` on any failure — config missing,
    token missing, GitHub HTTP error, unknown target name, or
    already-filed (idempotency guard). The caller is expected to surface
    the error message; no retries here.
    """
    cfg = PromotionConfig.from_network(network)
    if cfg is None:
        raise PromotionError(
            "intake.github not configured — run `evolve-admin intake configure`"
        )
    target = cfg.resolve(target_name)  # raises PromotionError on unknown name
    if not token or not token.strip():
        raise PromotionError(
            f"no token stored for keystore slot '{target.token_slot}' — "
            f"run `evolve-admin keys set {target.token_slot}`"
        )
    if intake.promotion.github_issue_url:
        raise PromotionError(
            f"intake already filed at {intake.promotion.github_issue_url}"
        )

    body, redactions = redact.build_issue_body(
        intake, include_transcript=include_transcript
    )
    title = _make_title(intake)
    labels = target.labels_by_kind.get(intake.kind, [])

    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels

    url = f"{GITHUB_API_BASE}/repos/{target.owner}/{target.repo}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token.strip()}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "evolve-intake",
        "Content-Type": "application/json",
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    tx = transport or _default_transport
    status, resp = tx("POST", url, headers, payload_bytes)

    if status != 201:
        msg = _format_error(status, resp)
        if status == 404:
            # GitHub returns 404 (not 401/403) for any private repo the token
            # can't see — to avoid leaking the repo's existence. Two distinct
            # bugs land here:
            #   (a) the PAT was created without `repo` scope. Token's own
            #       login matches the target owner, but every endpoint on
            #       the repo 404s. Common after picking "public_repo" from
            #       the intake-token modal when the target is private.
            #   (b) the PAT belongs to a different GitHub account that
            #       isn't a collaborator on the target. Token's /user
            #       login mismatches the target owner.
            # Probe /user once to disambiguate and rewrite the error
            # message so the operator knows what to rotate.
            hint = _diagnose_404(token.strip(), target.owner, target.repo, tx)
            if hint:
                msg = f"{msg} — {hint}"
        raise PromotionError(f"GitHub issue creation failed ({status}): {msg}")

    issue_url = str(resp.get("html_url") or "")
    issue_number = resp.get("number")
    if not isinstance(issue_number, int):
        issue_number = None

    intake.promotion.github_issue_url = issue_url or None
    intake.promotion.github_issue_number = issue_number
    intake.promotion.promoted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    intake.promotion.promoted_by = promoted_by
    intake.promotion.body_sent = body
    intake.promotion.redactions_applied = redactions

    note = f"filed → {issue_url or '(no url)'} (target={target.name})"
    return store.transition(
        intake,
        to="filed",
        shared_dir=shared_dir,
        actor=promoted_by,
        note=note,
    )


def _make_title(intake: Intake) -> str:
    """First line of the body, capped at 72 chars, prefixed by kind.

    GitHub issues need a title; admins write a body. Pulling the first
    sentence keeps titles legible without making admin author one.
    """
    first = (intake.body.strip().splitlines() or [""])[0].strip()
    if len(first) > 72:
        first = first[:69].rstrip() + "…"
    return f"[{intake.kind}] {first}"


def _format_error(status: int, resp: dict) -> str:
    """Best-effort error message extraction from a GitHub error response."""
    if not isinstance(resp, dict):
        return str(resp)
    msg = resp.get("message")
    errors = resp.get("errors")
    if msg and errors:
        return f"{msg}: {errors}"
    if msg:
        return str(msg)
    if "error" in resp:
        return str(resp["error"])
    return f"unexpected response: {str(resp)[:200]}"


def _diagnose_404(
    token: str, owner: str, repo: str, transport: Transport,
) -> str | None:
    """Best-effort one-line hint for a 404 from POST /repos/{owner}/{repo}/issues.

    Three cases worth distinguishing for the operator:
      - PAT lacks `repo` scope and the target is private. Self-login
        matches owner; GitHub hides every private-repo endpoint behind
        a 404 to avoid leaking existence.
      - PAT belongs to a different GitHub account. Self-login mismatches
        owner; the operator needs collaborator access or to rotate to a
        PAT from the right account.
      - PAT is revoked / expired. /user itself returns 401.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "evolve-intake-diag",
    }
    try:
        u_status, u_resp = transport("GET", f"{GITHUB_API_BASE}/user", headers, None)
    except Exception:  # noqa: BLE001
        return None
    if u_status == 401:
        return (
            "the PAT itself is rejected by GitHub (revoked or expired) — "
            "rotate it via Maintenance → Inbox → Rotate intake PAT"
        )
    if u_status != 200 or not isinstance(u_resp, dict):
        return None
    self_login = (u_resp.get("login") or "").strip()
    if not self_login:
        return None
    if self_login.lower() == owner.strip().lower():
        return (
            f"the PAT authenticates as @{self_login} but cannot see "
            f"{owner}/{repo}. Most likely the PAT is missing `repo` scope "
            f"(classic PATs without `repo` can't see private repos even when "
            f"they own them). Rotate via Maintenance → Inbox → Rotate intake "
            f"PAT and pick `repo` scope, or for fine-grained, add this repo "
            f"with `Contents: read+write` + `Issues: read+write`"
        )
    return (
        f"the PAT authenticates as @{self_login}, but the target {owner}/"
        f"{repo} is owned by @{owner}. You either need a PAT issued from "
        f"the @{owner} account, or @{self_login} needs collaborator access "
        f"to {owner}/{repo}"
    )
