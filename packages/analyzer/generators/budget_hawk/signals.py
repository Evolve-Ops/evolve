"""generators.budget_hawk.signals — Signal spec factories for Budget Hawk.

Threshold crossings (warn cap, cost anomaly) route to the Signal store,
not the Proposal queue.  These are observations — "something to look at"
— not prescriptions.  Each factory returns a kwargs dict ready to unpack
into ``signals.store.observe(**spec)``.

``config_hint`` is included on warn-cap signals so the Alerts UI card can
surface a direct "Adjust threshold" shortcut without the operator having
to navigate to the Generators tab manually.
"""

from __future__ import annotations

from schema.signal import make_signature

PRODUCER = "budget_hawk"


def warn_cap_signal_kwargs(
    bot_id: str,
    *,
    current_usd: float,
    cap_usd: float,
) -> dict:
    """Signal spec for a daily warn-cap crossing."""
    return dict(
        signature=make_signature(PRODUCER, "warn_cap_crossed", bot_id),
        producer=PRODUCER,
        type="warn_cap_crossed",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=(
            f"{bot_id} daily spend ${current_usd:.2f} crossed "
            f"warn cap ${cap_usd:.2f}"
        ),
        body=(
            f"Bot {bot_id}'s daily spend (${current_usd:.2f}) exceeded the "
            f"configured warn cap (${cap_usd:.2f}). Review recent usage or "
            "adjust the threshold if this reflects expected activity."
        ),
        details=dict(current_usd=current_usd, cap_usd=cap_usd),
        config_hint=dict(
            generator="budget_hawk",
            param="per_bot_daily_warn_usd",
            bot_id=bot_id,
        ),
    )


def cost_anomaly_signal_kwargs(
    bot_id: str,
    *,
    current_usd: float,
    mean_usd: float,
    stdevs: float,
) -> dict:
    """Signal spec for a statistical cost anomaly (spike vs. rolling mean)."""
    return dict(
        signature=make_signature(PRODUCER, "cost_anomaly", bot_id),
        producer=PRODUCER,
        type="cost_anomaly",
        flavor="activity",
        severity="warn",
        scope="bot",
        bot_id=bot_id,
        title=(
            f"{bot_id} cost anomaly: ${current_usd:.2f} vs "
            f"${mean_usd:.2f} mean (+{stdevs:.1f}\N{GREEK SMALL LETTER SIGMA})"
        ),
        body=(
            f"Bot {bot_id}'s daily spend (${current_usd:.2f}) is "
            f"{stdevs:.1f} standard deviations above its rolling mean "
            f"(${mean_usd:.2f}). This may reflect a legitimate spike or a "
            "runaway session."
        ),
        details=dict(current_usd=current_usd, mean_usd=mean_usd, stdevs=stdevs),
        config_hint=None,
    )
