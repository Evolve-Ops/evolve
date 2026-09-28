"""Atlas — shared library for daily-digest, article-capture, on-demand-research, weekly-recap.

All modules use only the Python stdlib (urllib, json, xml.etree, sqlite3 not used).
No external SDKs. No pip installs. Designed to run as the `atlas` bot user on macOS.

Public API per module:
- config        — read network.json / openclaw.json / per-bot capability config
- fetchers      — RSS, GitHub releases, Brave Search, generic URL fetch
- classifier    — 5-bucket LLM classifier (direct Anthropic API call)
- archive       — read/write archive/index.json + per-item Markdown files
- composer      — Team_bot_a-style daily digest and weekly recap composition
- hashing       — salted member-ID hashing (privacy)
- telegram_api  — direct Telegram Bot API calls (sendMessage, setMessageReaction)

Conventions:
- All functions return values; they do not exit on error. Callers decide.
- Errors are logged to stderr with a `[atlas:<module>]` prefix.
- All paths are relative to the bot workspace root unless absolute.
"""

BUCKETS = (
    "competitive_landscape",
    "new_tools",
    "use_cases",
    "case_studies",
    "warnings",
)

BUCKET_EMOJI = {
    "competitive_landscape": "⚔️",
    "new_tools": "🛠",
    "use_cases": "🏆",
    "case_studies": "📚",
    "warnings": "⚠️",
}

__all__ = ["BUCKETS", "BUCKET_EMOJI"]
