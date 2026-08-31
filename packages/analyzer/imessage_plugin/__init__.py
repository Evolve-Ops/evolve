"""imessage_plugin — iMessage primary-channel plugin (V2.1-6, Layer 2).

Polls chat.db for new incoming messages and forwards them to the bot's
OpenClaw gateway as turn events. Only runs for bots configured with
iMessage as their primary channel.

Entry points:
  python3 -m imessage_plugin.poller --bot-id <bot_id> --port <port>
"""
