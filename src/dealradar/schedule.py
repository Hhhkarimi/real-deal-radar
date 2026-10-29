"""Local monitoring hours, checked again when a scheduled run actually starts."""
import datetime as dt
from zoneinfo import ZoneInfo


def monitoring_allowed(config, now=None):
    start = config.get("monitoring_start_hour", 0)
    end = config.get("monitoring_end_hour", 24)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)) or not 0 <= start < end <= 24:
        raise ValueError("Monitoring hours must be integers with 0 <= start < end <= 24")
    now = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Monitoring time must include a timezone")
    local = now.astimezone(ZoneInfo(config.get("timezone", "Asia/Tehran")))
    return start <= local.hour < end
