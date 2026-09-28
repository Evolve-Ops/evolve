"""
errors.py — Exception types for the bot template framework.

Kept in its own module so loader/validator/resolver/provisioner can import
without cycles.
"""

from __future__ import annotations


class TemplateError(Exception):
    """Base class for bot-template framework errors."""


class TemplateNotFoundError(TemplateError):
    """Raised when a template name cannot be located on disk."""


class TemplateValidationError(TemplateError):
    """Raised when a template fails schema or invariant validation.

    The ``issues`` attribute is the list of human-readable problems found.
    """

    def __init__(self, template_name: str, issues: list[str]):
        self.template_name = template_name
        self.issues = list(issues)
        msg = (
            f"template {template_name!r} is invalid:\n  - "
            + "\n  - ".join(self.issues)
        )
        super().__init__(msg)


class SkillResolutionError(TemplateError):
    """Raised when a skill declared by a template cannot be resolved.

    "Cannot be resolved" means: not installed on the bot AND no install
    pathway is known. (Missing-but-installable skills are queued, not
    raised.)
    """
