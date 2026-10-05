"""Per-bot messaging-ID pairing for member bots.

Surfaces the OC pairing flow (DM the bot → bot replies with code →
operator confirms identity) as a dedicated admin-UI wizard, distinct
from bot provisioning. The install wizard's job ends at "bot
provisioned + gateway healthy"; pairing is its own beat.

Modules:
  config        per-channel UI/validation table
  (routes live in ``evolve_admin.web.routes_pairing``)

Spec: pairing flow design discussion 2026-06-01.
"""
