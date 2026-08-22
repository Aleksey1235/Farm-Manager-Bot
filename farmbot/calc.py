from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WeekBounds:
    start: date
    end: date


def week_bounds(day: date) -> WeekBounds:
    start = day - timedelta(days=day.weekday())
    return WeekBounds(start=start, end=start + timedelta(days=6))


def seconds_between(start_iso: str, end_iso: str | None, now: datetime) -> int:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso) if end_iso else now
    return max(0, int((end - start).total_seconds()))


def daily_charge(total_seconds: int, threshold_minutes: int, auto_amount: int,
                 manual_amount: int | None) -> tuple[str, int | None]:
    if total_seconds >= threshold_minutes * 60:
        return "auto", auto_amount
    if manual_amount is not None:
        return "manual", manual_amount
    return "pending", None


def weekly_due(day_amounts: list[int | None], approved_contents: int,
               discount_per_content: int, minimum_weekly_due: int) -> tuple[int | None, int]:
    if any(x is None for x in day_amounts):
        return None, approved_contents * discount_per_content
    gross = sum(int(x) for x in day_amounts)
    discount = approved_contents * discount_per_content
    return max(minimum_weekly_due, gross - discount), discount


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def percent_change(current: int | float, previous: int | float) -> float | None:
    if previous == 0:
        return None if current != 0 else 0.0
    return ((current - previous) / previous) * 100.0


def latest_report_week_start(now: datetime, weekday: int, hour: int, minute: int) -> date:
    """Return the week whose scheduled report time is the most recent one <= now.

    weekday: Monday=0 ... Sunday=6.
    """
    current = week_bounds(now.date()).start
    scheduled_day = current + timedelta(days=weekday)
    scheduled = datetime.combine(scheduled_day, time(hour=hour, minute=minute), tzinfo=now.tzinfo)
    if now >= scheduled:
        return current
    return current - timedelta(days=7)
