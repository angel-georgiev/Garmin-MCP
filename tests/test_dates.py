from datetime import date, timedelta

import pytest

from garmin_mcp.dates import DateParseError, iter_dates, parse_date, parse_range


def iso(delta_days: int = 0) -> str:
    return (date.today() + timedelta(days=delta_days)).isoformat()


def test_keywords():
    assert parse_date("today") == iso()
    assert parse_date("Yesterday") == iso(-1)
    assert parse_date("tomorrow") == iso(1)


def test_iso_passthrough():
    assert parse_date("2026-08-01") == "2026-08-01"


def test_none_uses_default():
    assert parse_date(None) == iso()
    assert parse_date("  ") == iso()
    assert parse_date(None, default=date(2020, 1, 2)) == "2020-01-02"


@pytest.mark.parametrize(
    ("text", "days"),
    [("-7d", -7), ("+3d", 3), ("-2w", -14), ("-1m", -30), ("7 days ago", -7), ("2 weeks ago", -14)],
)
def test_relative(text, days):
    assert parse_date(text) == iso(days)


def test_bad_date():
    with pytest.raises(DateParseError):
        parse_date("next tuesday-ish")


def test_range_defaults_to_last_week():
    start, end = parse_range(None, None)
    assert end == iso()
    assert start == iso(-6)


def test_range_explicit():
    assert parse_range("2026-01-01", "2026-01-31") == ("2026-01-01", "2026-01-31")


def test_range_rejects_inverted():
    with pytest.raises(DateParseError):
        parse_range("2026-02-01", "2026-01-01")


def test_range_rejects_too_wide():
    with pytest.raises(DateParseError):
        parse_range("2020-01-01", "2026-01-01")


def test_iter_dates():
    assert list(iter_dates("2026-01-30", "2026-02-02")) == [
        "2026-01-30",
        "2026-01-31",
        "2026-02-01",
        "2026-02-02",
    ]
