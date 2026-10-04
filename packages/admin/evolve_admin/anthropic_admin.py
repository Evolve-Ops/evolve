"""evolve_admin.anthropic_admin — Anthropic Admin API client.

Tier 2.2 of the OpenClaw admin coverage roadmap. Phase A: on-demand
fetch of Anthropic's `/v1/organizations/cost_report` as a cross-check
against Evolve's locally-derived cost ledger.

**Credentials.** The Admin API requires an org-scoped admin key
(distinct from the per-bot inference keys evolve uses for synthesis
and warden work). Operator mints the key in console.anthropic.com and
drops it as JSON at::

    {shared_dir}/anthropic-admin-key.json     # mode 600, evolve-owned

    {
      "api_key": "sk-ant-admin-..."
    }

Loader is lenient about the JSON shape — also accepts a bare string
(``"sk-ant-admin-..."``) or a top-level ``ANTHROPIC_ADMIN_API_KEY``
env var. Returns ``None`` when nothing is configured so the UI can
surface "key missing" instead of crashing.

**Phase B (later).** Daily ingest, audit-log fetching, divergence
signal when local ledger and Anthropic totals disagree.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger(__name__)


# Anthropic Admin API base URL. Exposed as a module attribute so tests
# can stub it (we use mocked transport, not a real URL, in tests).
ANTHROPIC_API_BASE = "https://api.anthropic.com"

# API version the admin endpoints require. Bumping requires
# coordinated review of the response shape.
ANTHROPIC_API_VERSION = "2023-06-01"

# Default request timeout — admin endpoints can be slow on large
# orgs but a single Phase A request should resolve in seconds.
DEFAULT_TIMEOUT_S = 30


# ─────────────────────────────────────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────────────────────────────────────


_KEY_FILENAME = "anthropic-admin-key.json"
_ENV_VAR = "ANTHROPIC_ADMIN_API_KEY"


def admin_key_path(shared_dir: Path) -> Path:
    return Path(shared_dir) / _KEY_FILENAME


def load_admin_api_key(shared_dir: Path) -> str | None:
    """Resolve the Anthropic admin API key.

    Order of resolution:

      1. ``{shared_dir}/anthropic-admin-key.json`` — preferred. Accepts
         either ``{"api_key": "..."}`` or a bare JSON string.
      2. ``ANTHROPIC_ADMIN_API_KEY`` env var — fallback for dev/test.

    Returns ``None`` when neither source has a usable key. The UI
    surfaces this as "not configured" rather than treating it as an
    error.
    """
    p = admin_key_path(shared_dir)
    if p.exists():
        try:
            raw = p.read_text(encoding="utf-8").strip()
            if not raw:
                return _from_env()
            # Accept bare-string or {"api_key": "..."}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Treat the raw file as the key itself if it doesn't
                # parse — tolerant for the operator dropping just the
                # key string.
                return raw or _from_env()
            if isinstance(data, str):
                return data.strip() or _from_env()
            if isinstance(data, dict):
                k = data.get("api_key") or data.get("key")
                if isinstance(k, str) and k.strip():
                    return k.strip()
        except OSError as exc:
            log.warning("admin key file unreadable: %s", exc)
    return _from_env()


def _from_env() -> str | None:
    v = os.environ.get(_ENV_VAR, "").strip()
    return v or None


# Auth-profile locations to check when detecting whether a bot is wired up
# with Anthropic credentials. Mirrors ``primary_bot.primary_bot_auth_profile_paths``
# but applied per-bot — we need pod-wide coverage, not just the primary.
_AUTH_PROFILE_RELS = (
    ".openclaw/agents/main/agent/auth-profiles.json",
    ".openclaw/agents/main/auth-profiles.json",
    ".openclaw/auth-profiles.json",
)


def pod_uses_anthropic(network: dict, users_root: Path | str = "/Users") -> bool:
    """Return True if any bot in the pod has an Anthropic key configured.

    Walks every bot in ``network.bots``, checks each candidate auth-profile
    location, and uses the same lenient extractor as ``primary_bot`` so
    every JSON shape it tolerates (canonical, flat, ultra-flat, list) is
    covered here too.

    The cross-check UI uses this to hide itself entirely on pods that
    don't run any Anthropic-backed bot — the Admin API cost report is
    only meaningful in that case.

    ``users_root`` is the directory under which each bot's home lives;
    defaults to ``/Users`` (macOS convention used on the mini). Tests
    override this to point at a tmp tree.
    """
    bots = network.get("bots") or {}
    if not isinstance(bots, dict):
        return False
    try:
        from primary_bot import _extract_anthropic_key  # type: ignore
    except Exception:  # pragma: no cover - analyzer pkg not importable
        _extract_anthropic_key = None  # type: ignore
    root = Path(users_root)
    for bot_id, cfg in bots.items():
        user = (cfg or {}).get("user") if isinstance(cfg, dict) else None
        user = user or bot_id
        home = root / user
        for rel in _AUTH_PROFILE_RELS:
            p = home / rel
            try:
                raw_text = p.read_text(encoding="utf-8")
            except (OSError, PermissionError):
                continue
            try:
                raw = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if _extract_anthropic_key is not None:
                if _extract_anthropic_key(raw):
                    return True
            elif _shallow_contains_anthropic(raw):
                return True
    return False


def _shallow_contains_anthropic(raw) -> bool:
    """Fallback detector if ``primary_bot`` isn't importable."""
    if isinstance(raw, dict):
        profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else raw
        for pid, pdata in (profiles or {}).items():
            if "anthropic" not in str(pid).lower():
                continue
            if isinstance(pdata, str) and pdata.strip():
                return True
            if isinstance(pdata, dict):
                for fld in ("api_key", "key", "token", "value"):
                    v = pdata.get(fld)
                    if isinstance(v, str) and v.strip():
                        return True
    return False


def save_admin_api_key(shared_dir: Path, api_key: str) -> None:
    """Atomically write the admin key to ``{shared_dir}/anthropic-admin-key.json``.

    Writes mode 600 via temp-file + rename. Callers must validate the
    key's shape before calling (the route handler enforces the
    ``sk-ant-admin-`` prefix). Overwrites any existing key file.

    Raises ``OSError`` on filesystem failure so callers can map to an HTTP
    error rather than silently swallowing.
    """
    dest = admin_key_path(shared_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    payload = json.dumps({"api_key": api_key.strip()}, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP transport — stubbable for tests
# ─────────────────────────────────────────────────────────────────────────────


# Tests inject their own transport instead of patching urllib globally.
# Signature: (method, url, headers, body) -> (status_code, response_dict)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict]]


def _default_transport(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, dict]:
    """Default urllib-backed transport."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AnthropicAdminError:
    status: int
    message: str
    body: dict


@dataclass
class CostReport:
    """Normalized cost-report response.

    The Anthropic API returns a ``data`` array of time-bucketed rows
    with model / workspace dimensions. We pass the raw payload
    through to the UI (operators want to see exactly what Anthropic
    sent), plus a few derived fields the UI commonly needs.
    """

    bucket_width: str
    starting_at: str
    ending_at: str
    total_cost_usd: float
    bucket_count: int
    raw: dict

    def to_dict(self) -> dict:
        return {
            "bucket_width": self.bucket_width,
            "starting_at": self.starting_at,
            "ending_at": self.ending_at,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "bucket_count": self.bucket_count,
            "raw": self.raw,
        }


def fetch_cost_report(
    api_key: str,
    *,
    starting_at: str,
    ending_at: str,
    bucket_width: str = "1d",
    transport: Transport | None = None,
) -> tuple[CostReport | None, AnthropicAdminError | None]:
    """Fetch the org-level cost report.

    ``starting_at`` and ``ending_at`` are ISO-8601 strings. The Admin
    API expects RFC3339 / ISO-8601 with timezone; passing date-only
    strings (``2026-05-04``) gets normalized to UTC midnight by
    Anthropic.

    Returns ``(report, None)`` on success or ``(None, error)`` on
    any failure (network, auth, malformed response). Never raises.
    """
    transport = transport or _default_transport
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "bucket_width": bucket_width,
    }
    url = (
        f"{ANTHROPIC_API_BASE}/v1/organizations/cost_report"
        + "?"
        + urllib.parse.urlencode(params)
    )
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    status, payload = transport("GET", url, headers, None)
    if status != 200:
        msg = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("type") or "")
            elif isinstance(err, str):
                msg = err
            else:
                msg = payload.get("message") or ""
        return None, AnthropicAdminError(
            status=status,
            message=msg or f"HTTP {status}",
            body=payload if isinstance(payload, dict) else {"body": str(payload)},
        )

    data = payload.get("data") if isinstance(payload, dict) else None
    buckets = data if isinstance(data, list) else []
    total = _sum_cost(buckets)
    return (
        CostReport(
            bucket_width=bucket_width,
            starting_at=starting_at,
            ending_at=ending_at,
            total_cost_usd=total,
            bucket_count=len(buckets),
            raw=payload,
        ),
        None,
    )


def _sum_cost(buckets: list[Any]) -> float:
    """Sum the cost across rows. Different API responses package this
    field differently; try a few shapes."""
    total = 0.0
    for b in buckets:
        if not isinstance(b, dict):
            continue
        # Common shape: each bucket has a list of results with amounts
        for key in ("results", "data"):
            rows = b.get(key)
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    total += _extract_amount(row)
                break
        else:
            # Bucket itself carries the amount
            total += _extract_amount(b)
    return total


def _extract_amount(row: dict) -> float:
    """Pull a USD amount from a cost row. Tolerant of nested
    ``amount.currency/amount.value`` and flat ``cost_usd``."""
    for key in ("cost_usd", "amount_usd", "amount"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            inner = v.get("value")
            if isinstance(inner, (int, float)):
                return float(inner)
            if isinstance(inner, str):
                try:
                    return float(inner)
                except ValueError:
                    pass
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Audit logs (Phase B)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AuditLogPage:
    """A page of audit-log events from Anthropic's admin API.

    Anthropic's API paginates with ``next_page`` cursors; for Phase B
    we capture one page (default 100 events) per ingest — enough for
    a daily snapshot. Operators rarely care about audit-event volume
    that would require multi-page fetches in a single day.
    """

    starting_at: str
    ending_at: str
    event_count: int
    raw: dict

    def events(self) -> list[dict]:
        data = self.raw.get("data") if isinstance(self.raw, dict) else None
        return data if isinstance(data, list) else []

    def to_dict(self) -> dict:
        return {
            "starting_at": self.starting_at,
            "ending_at": self.ending_at,
            "event_count": self.event_count,
            "raw": self.raw,
        }


def fetch_audit_logs(
    api_key: str,
    *,
    starting_at: str,
    ending_at: str,
    limit: int = 100,
    transport: Transport | None = None,
) -> tuple[AuditLogPage | None, AnthropicAdminError | None]:
    """Fetch the org-level audit log.

    Returns one page of events (up to ``limit``, capped at 100 per the
    Admin API). Phase B-1 is single-page only; multi-page pagination
    can land later when an operator hits a day with >100 events.
    """
    transport = transport or _default_transport
    limit = max(1, min(100, int(limit)))
    params = {
        "starting_at": starting_at,
        "ending_at": ending_at,
        "limit": str(limit),
    }
    url = (
        f"{ANTHROPIC_API_BASE}/v1/organizations/audit_logs"
        + "?"
        + urllib.parse.urlencode(params)
    )
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    status, payload = transport("GET", url, headers, None)
    if status != 200:
        msg = ""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("type") or "")
            elif isinstance(err, str):
                msg = err
            else:
                msg = payload.get("message") or ""
        return None, AnthropicAdminError(
            status=status,
            message=msg or f"HTTP {status}",
            body=payload if isinstance(payload, dict) else {"body": str(payload)},
        )

    data = payload.get("data") if isinstance(payload, dict) else None
    events = data if isinstance(data, list) else []
    return (
        AuditLogPage(
            starting_at=starting_at,
            ending_at=ending_at,
            event_count=len(events),
            raw=payload,
        ),
        None,
    )
