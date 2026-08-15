"""Lenient date parsing so tools accept "today", "-7d" or "2026-08-01"."""

from __future__ import annotations

import re
from datetime import date, timedelta

ISO_FORMAT = "%Y-%m-%d"

_RELATIVE_RE = re.compile(r"^([+-]?\d+)\s*(d|day|days|w|week|weeks|m|month|months|y|year|years)$")
_AGO_RE = re.compile(r"^(\d+)\s*(d|day|days|w|week|weeks|m|month|months|y|year|years)\s+ago$")

_UNIT_DAYS = {
    "d": 1,
    "day": 1,
    "days": 1,
    "w": 7,
    "week": 7,
    "weeks": 7,
    "m": 30,
    "month": 30,
    "months": 30,
    "y": 365,
    "year": 365,
    "years": 365,
}


class DateParseError(ValueError):
    """Raised when a date argument cannot be understood."""


def today() -> date:
    return date.today()


def parse_date(value: str | None, *, default: date | None = None) -> str:
    """Return ``YYYY-MM-DD`` for a user-supplied date expression.

    Accepts ISO dates (``2026-08-01``), the words ``today``/``yesterday``/
    ``tomorrow``, offsets like ``-7d`` / ``-3w`` / ``-2m``, and phrases like
    ``7 days ago``. ``None`` or an empty string resolves to ``default``
    (today when no default is given).
    """
    base = default or today()
    if value is None:
        return base.strftime(ISO_FORMAT)

    text = value.strip().lower()
    if not text:
        return base.strftime(ISO_FORMAT)

    if text == "today":
        return today().strftime(ISO_FORMAT)
    if text == "yesterday":
        return (today() - timedelta(days=1)).strftime(ISO_FORMAT)
    if text == "tomorrow":
        return (today() + timedelta(days=1)).strftime(ISO_FORMAT)

    try:
        return date.fromisoformat(text).strftime(ISO_FORMAT)
    except ValueError:
        pass

    match = _RELATIVE_RE.match(text)
    if match:
        amount = int(match.group(1))
        return (today() + timedelta(days=amount * _UNIT_DAYS[match.group(2)])).strftime(ISO_FORMAT)

    match = _AGO_RE.match(text)
    if match:
        amount = int(match.group(1))
        return (today() - timedelta(days=amount * _UNIT_DAYS[match.group(2)])).strftime(ISO_FORMAT)

    raise DateParseError(
        f"Could not parse date {value!r}. Use YYYY-MM-DD, 'today', 'yesterday', "
        "an offset like '-7d', or a phrase like '30 days ago'."
    )


def parse_range(
    start_date: str | None,
    end_date: str | None,
    *,
    default_days: int = 7,
    max_days: int = 366,
) -> tuple[str, str]:
    """Resolve a start/end pair into two ISO dates.

    Defaults to the last ``default_days`` days ending today. Raises on an
    inverted range or a span longer than ``max_days`` (Garmin rejects or
    throttles very wide windows, and the response would blow past any sane
    context budget anyway).
    """
    end = date.fromisoformat(parse_date(end_date))
    if start_date is None or not start_date.strip():
        start = end - timedelta(days=default_days - 1)
    else:
        start = date.fromisoformat(parse_date(start_date))

    if start > end:
        raise DateParseError(f"start_date {start} is after end_date {end}.")

    span = (end - start).days + 1
    if span > max_days:
        raise DateParseError(
            f"Range of {span} days is too wide (max {max_days}). "
            "Request a shorter window, or call the tool several times."
        )
    return start.strftime(ISO_FORMAT), end.strftime(ISO_FORMAT)


def iter_dates(start: str, end: str):
    """Yield each ISO date from ``start`` to ``end`` inclusive."""
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        yield current.strftime(ISO_FORMAT)
        current += timedelta(days=1)
