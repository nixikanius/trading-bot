from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta


def format_duration(td: timedelta) -> str:
    """Format duration in a human-readable format"""

    total_seconds = int(td.total_seconds())
    sign = "-" if total_seconds < 0 else ""
    total_seconds = abs(total_seconds)

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return sign + "".join(parts)


def price_decimals(min_price_step: float) -> int:
    """Return the number of decimal places used by an instrument's price step."""
    step = Decimal(str(min_price_step)).normalize()
    return max(0, -step.as_tuple().exponent)

def format_price(value: float, min_price_step: float) -> str:
    """Round and render a price with the instrument's fixed precision."""
    decimals = price_decimals(min_price_step)
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{rounded:.{decimals}f}"
