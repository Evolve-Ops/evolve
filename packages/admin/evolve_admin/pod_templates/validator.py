"""
validator.py — Invariant checks on a loaded :class:`PodTemplate`.

Separated from the loader so callers can load-and-validate eagerly or
inspect a partial/broken template without raising (same split as
``bot_templates.validator``).

Two classes of check:

  * **Structural** — name shape, unique bot ids, known release mode, bot
    roster sanity. These are the "can this be provisioned?" checks.
  * **Secret-leak** — a defense-in-depth sweep over the *entire* template
    body for credential-shaped keys/values. A pod-template captures shape,
    never secrets; the extractor scrubs on capture, but a leaked token is
    severe enough that the validator independently refuses it. This is an
    **error**, not a warning — a pod-template carrying a secret must never
    validate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import PodTemplateValidationError
from .loader import POD_TEMPLATE_SCHEMA_VERSION, PodTemplate


# ── Constants ─────────────────────────────────────────────────────────────────

# Lowercase kebab/snake with a leading letter — same shape bot-templates,
# ClawHub, and the gallery pkg space use.
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Release modes the deploy pipeline understands (release_manager.py).
_KNOWN_RELEASE_MODES = frozenset({"canary", "direct"})

# Key-name fragments that mark a value as a credential. Matched
# case-insensitively as substrings of a key. Kept deliberately broad — a
# false positive here costs an operator one rename; a false negative ships
# a secret in a shared artifact.
_SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passphrase",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "client_secret",
    "credential",
    "auth_profile",
    "bearer",
    "session_cookie",
    # Bare "key" / "pat" are matched with word-boundary rules below to
    # avoid flagging legitimate fields like "model_keys" mapping shapes.
)

# Value patterns that look like leaked secrets even under an innocent key
# name (e.g. a GitHub PAT pasted into a "note" field). Conservative — only
# high-signal, vendor-prefixed shapes to keep false positives near zero.
_SECRET_VALUE_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),       # GitHub PAT / token
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),            # OpenAI-style key
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),        # Anthropic key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),     # Slack token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),               # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private key
    # scheme://user:pass@host — credentials embedded in a connection string,
    # the one free-text-under-innocent-key shape common enough to be worth a
    # high-signal (near-zero-FP) match even outside a secret-named key.
    re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@"),
)


# ── Report ────────────────────────────────────────────────────────────────────


@dataclass
class ValidationReport:
    """Collected errors + warnings from validating a pod-template.

    ``errors`` block provisioning; ``warnings`` are informational.
    """

    template_name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise PodTemplateValidationError(self.template_name, self.errors)


# ── Validator ─────────────────────────────────────────────────────────────────


def validate_pod_template(template: PodTemplate) -> ValidationReport:
    """Walk a loaded pod-template, accumulating structural + safety problems.

    Checks:
      1. Name is a valid identifier.
      2. ``description`` present (warning).
      3. ``schema_version`` not from the future (warning — best-effort load).
      4. Bot ids unique + valid identifiers; roster non-empty (warning).
      5. Exactly one ``role: primary`` (warning otherwise — a pod usually
         has one primary bot for alerts/proposals).
      6. ``release_mode`` is a known mode (warning if not).
      7. **No secrets anywhere in the body** (error).
    """
    r = ValidationReport(template_name=template.name)

    # 1. Name shape
    if not _ID_RE.match(template.name):
        r.errors.append(
            f"pod-template name {template.name!r} must match {_ID_RE.pattern!r} "
            f"(lowercase, leading letter, digits/dash/underscore only)"
        )

    # 2. Description
    if not template.description:
        r.warnings.append(
            "pod-template has no 'description' — UI/CLI cannot present a summary"
        )

    # 3. Schema version
    if template.schema_version > POD_TEMPLATE_SCHEMA_VERSION:
        r.warnings.append(
            f"schema_version {template.schema_version} is newer than this "
            f"loader supports ({POD_TEMPLATE_SCHEMA_VERSION}) — fields it does "
            f"not understand were preserved but ignored"
        )

    # 4/5. Bot roster
    if not template.bots:
        r.warnings.append(
            "pod-template has no bots — provisioning it would create an empty pod"
        )
    seen_ids: set[str] = set()
    primary_count = 0
    for i, bot in enumerate(template.bots):
        if bot.bot_id in seen_ids:
            r.errors.append(f"bot id {bot.bot_id!r} declared more than once")
        seen_ids.add(bot.bot_id)
        if not _ID_RE.match(bot.bot_id):
            r.errors.append(
                f"bots[{i}].bot_id {bot.bot_id!r} must match {_ID_RE.pattern!r}"
            )
        if (bot.role or "").lower() == "primary":
            primary_count += 1
    if template.bots and primary_count == 0:
        r.warnings.append(
            "no bot has role 'primary' — the pod will have no bot to receive "
            "sysadmin alerts / proposals"
        )
    elif primary_count > 1:
        r.warnings.append(
            f"{primary_count} bots have role 'primary' — a pod normally has one"
        )

    # 6. Release mode
    rm = template.pod.release_mode
    if rm is not None and rm not in _KNOWN_RELEASE_MODES:
        r.warnings.append(
            f"pod.release_mode {rm!r} not in known modes "
            f"({sorted(_KNOWN_RELEASE_MODES)}) — taken as advisory"
        )

    # 7. Secret-leak sweep over the whole serialized body.
    for path, reason in find_secrets(template.to_payload()):
        r.errors.append(
            f"possible secret in pod-template body at {path}: {reason}. "
            f"Pod-templates capture shape, never credentials — remove it."
        )

    return r


# ── Secret scanning ─────────────────────────────────────────────────────────────


def find_secrets(value: Any, *, _path: str = "$") -> list[tuple[str, str]]:
    """Recursively scan a JSON-ish value for credential-shaped data.

    Returns a list of ``(json_path, reason)`` for every suspected secret.
    Used by the validator (error on any hit) and by the extractor as a
    post-scrub assertion (fail the capture rather than emit an unsafe file).

    Matches two ways:
      * a **key name** containing a secret fragment (the value is then
        treated as sensitive regardless of content);
      * a **string value** matching a high-signal vendor secret pattern,
        even under an innocent key.
    """
    hits: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            key_str = str(k)
            child = f"{_path}.{key_str}"
            if is_secret_key(key_str) and _nonempty_scalar(v):
                hits.append((child, f"key name {key_str!r} looks like a credential"))
            hits.extend(find_secrets(v, _path=child))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            hits.extend(find_secrets(item, _path=f"{_path}[{idx}]"))
    elif isinstance(value, str):
        if value_looks_secret(value):
            hits.append((_path, "value matches a known secret pattern"))
    return hits


def is_secret_key(key: str) -> bool:
    """True if a key *name* marks its value as a credential.

    Public so the extractor's scrubber and the validator share one
    definition of "looks like a secret key".
    """
    low = key.lower()
    if any(frag in low for frag in _SECRET_KEY_FRAGMENTS):
        return True
    # Word-boundary check for the ambiguous short fragments so "key"/"pat"
    # flag "api key"/"my_pat" but not "model_keys"/"compatible"/"pattern".
    return bool(re.search(r"(?:^|[_\-])(key|pat)(?:$|[_\-])", low))


def value_looks_secret(text: str) -> bool:
    """True if a string *value* matches a high-signal vendor secret shape."""
    return any(rx.search(text) for rx in _SECRET_VALUE_RES)


def _nonempty_scalar(v: Any) -> bool:
    """A secret-named key only matters if it actually holds a value.

    Empty strings / None / empty containers are shape, not leaks — a
    captured ``{"token": ""}`` placeholder is fine; ``{"token": "abc123"}``
    is not.
    """
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True
