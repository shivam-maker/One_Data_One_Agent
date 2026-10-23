"""Time helpers. All internal times are timezone-aware UTC datetimes; IST is only used for policy rules."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def in_quiet_hours(dt: datetime) -> bool:
    """Messaging quiet hours are 22:00-08:00 IST."""
    h = dt.astimezone(IST).hour
    return h >= 22 or h < 8


def at_ist(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def next_send_slot(now: datetime) -> datetime:
    """Follow-up nudge time: 19:00 IST today if at least 2h away, else 12:30 IST tomorrow.

    Both slots are outside quiet hours by construction; the verifier still re-checks."""
    day = ist_day(now)
    evening = at_ist(day, 19, 0)
    if evening - now >= timedelta(hours=2):
        return evening
    return at_ist(day + timedelta(days=1), 12, 30)
