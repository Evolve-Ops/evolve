"""
errors.py — Exception types for the pod-template framework.

Kept in its own module so loader/validator/extractor can import without
cycles (mirrors ``bot_templates.errors``).
"""

from __future__ import annotations


class PodTemplateError(Exception):
    """Base class for pod-template framework errors."""


class PodTemplateNotFoundError(PodTemplateError):
    """Raised when a pod-template name cannot be located on disk."""


class PodTemplateValidationError(PodTemplateError):
    """Raised when a pod-template fails schema or invariant validation.

    The ``issues`` attribute is the list of human-readable problems found.
    """

    def __init__(self, template_name: str, issues: list[str]):
        self.template_name = template_name
        self.issues = list(issues)
        msg = (
            f"pod-template {template_name!r} is invalid:\n  - "
            + "\n  - ".join(self.issues)
        )
        super().__init__(msg)


class PodTemplateExtractError(PodTemplateError):
    """Raised when capturing a pod-template from a live pod fails.

    The most common cause is a malformed ``network.json`` (no ``bots``
    roster), but it also covers a secret leaking past the scrubber — the
    extractor treats that as a hard failure rather than emitting an unsafe
    artifact.
    """
