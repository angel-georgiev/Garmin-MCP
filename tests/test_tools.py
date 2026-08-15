"""Tool-level tests driven by a fake Garmin client — no network, no credentials."""

import asyncio
import json
from datetime import date

import pytest

from garmin_mcp import server
from garmin_mcp.session import GarminError


class FakeGarmin:
    """Records the calls a tool makes and returns canned payloads."""

    full_name = "Test User"
    display_name = "testuser"
    unit_system = "metric"

    def __init__(self, **payloads):
        self.payloads = payloads
        self.calls = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            payload = self.payloads.get(name)
            if isinstance(payload, Exception):
                raise payload
            return payload

        return method


class FakeSession:
    def __init__(self, client):
        self.client = client

    async def call(self, func):
        return func(self.client)

    async def login(self):
        return self.client


@pytest.fixture
def fake(monkeypatch):
    def _install(**payloads):
        client = FakeGarmin(**payloads)
        monkeypatch.setattr(server, "SESSION", FakeSession(client))
        return client

    return _install


def run(coro):
    return asyncio.run(coro)


def test_tools_are_registered():
    tools = run(server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"get_sleep", "list_activities", "get_daily_summary", "garmin_api_get"} <= names
    assert len(names) >= 25
    for tool in tools:
        assert tool.description, f"{tool.name} is missing a description"


def test_daily_summary_defaults_to_today(fake):
    client = fake(get_user_summary={"totalSteps": 8123})
    result = json.loads(run(server.get_daily_summary()))
    assert result == {"totalSteps": 8123}
    assert client.calls == [("get_user_summary", (date.today().isoformat(),), {})]


def test_daily_summary_accepts_relative_date(fake):
    client = fake(get_user_summary={})
    run(server.get_daily_summary("yesterday"))
    (_, args, _), = client.calls
    assert args[0] < date.today().isoformat()


def test_sleep_drops_detail_by_default(fake):
    payload = {
        "dailySleepDTO": {"sleepTimeSeconds": 27000},
        "sleepLevels": [{"startGMT": "..."} for _ in range(100)],
        "sleepMovement": [1, 2, 3],
    }
    fake(get_sleep_data=payload)
    trimmed = json.loads(run(server.get_sleep("2026-08-01")))
    assert trimmed == {"dailySleepDTO": {"sleepTimeSeconds": 27000}}

    full = json.loads(run(server.get_sleep("2026-08-01", include_detail=True)))
    assert len(full["sleepLevels"]) == 100


def test_list_activities_returns_compact_summaries(fake):
    activity = {
        "activityId": 42,
        "activityName": "Morning Run",
        "activityType": {"typeKey": "running", "typeId": 1, "parentTypeId": 17},
        "distance": 10000.0,
        "duration": 3000.0,
        "averageHR": 145,
        "ownerProfileImageUrlLarge": "https://example.invalid/huge.png",
        "summarizedDiveInfo": {"weights": []},
    }
    client = fake(get_activities=[activity])
    result = json.loads(run(server.list_activities(limit=5)))
    assert result == [
        {
            "activityId": 42,
            "activityName": "Morning Run",
            "distance": 10000.0,
            "duration": 3000.0,
            "averageHR": 145,
            "activityType": "running",
        }
    ]
    assert client.calls == [("get_activities", (0, 5, None), {})]


def test_list_activities_full_detail_keeps_everything(fake):
    fake(get_activities=[{"activityId": 1, "junk": "kept"}])
    result = json.loads(run(server.list_activities(full_detail=True)))
    assert result[0]["junk"] == "kept"


def test_list_activities_by_date_range(fake):
    client = fake(get_activities_by_date=[])
    run(
        server.list_activities(
            start_date="2026-01-01", end_date="2026-01-31", activity_type="cycling"
        )
    )
    assert client.calls == [("get_activities_by_date", ("2026-01-01", "2026-01-31", "cycling"), {})]


def test_activity_details_clamps_point_count(fake):
    client = fake(get_activity_details={"ok": True})
    run(server.get_activity_details("42", max_points=99999))
    assert client.calls[0][2] == {"maxchart": 2000, "maxpoly": 2000}


def test_intensity_minutes_weekly_uses_a_range(fake):
    client = fake(get_weekly_intensity_minutes=[])
    run(server.get_intensity_minutes(weekly=True, start_date="2026-01-01", end_date="2026-01-28"))
    assert client.calls == [("get_weekly_intensity_minutes", ("2026-01-01", "2026-01-28"), {})]


def test_resting_heart_rate_picks_endpoint_by_span(fake):
    client = fake(get_rhr_day={}, get_rhr_daily=[])
    run(server.get_resting_heart_rate(start_date="2026-01-01", end_date="2026-01-01"))
    run(server.get_resting_heart_rate(start_date="2026-01-01", end_date="2026-01-05"))
    assert [call[0] for call in client.calls] == ["get_rhr_day", "get_rhr_daily"]


def test_fitness_metrics_tolerates_missing_metrics(fake):
    fake(
        get_max_metrics={"vo2Max": 52},
        get_fitnessage_data=RuntimeError("no fitness age"),
        get_endurance_score={"overallScore": 7000},
        get_hill_score={},
    )
    result = json.loads(run(server.get_fitness_metrics("2026-08-01")))
    assert result["max_metrics"] == {"vo2Max": 52}
    assert "no fitness age" in result["fitness_age"]["unavailable"]


def test_gear_resolves_profile_number(fake):
    client = fake(
        get_user_profile={"id": 12345},
        get_gear=[{"uuid": "abc", "displayName": "Shoes"}],
        get_gear_stats={"totalDistance": 500},
    )
    result = json.loads(run(server.get_gear(include_stats=True)))
    assert result[0]["stats"] == {"totalDistance": 500}
    assert ("get_gear", ("12345",), {}) in client.calls


def test_api_passthrough_requires_a_path(fake):
    fake(connectapi={})
    with pytest.raises(ValueError, match="must start with"):
        run(server.garmin_api_get("usersummary-service/foo"))
    with pytest.raises(ValueError, match="not a full URL"):
        run(server.garmin_api_get("https://connect.garmin.com/foo"))


def test_api_passthrough_forwards_params(fake):
    client = fake(connectapi={"ok": 1})
    run(server.garmin_api_get("/wellness-service/x", {"date": "2026-08-01"}))
    assert client.calls == [
        ("connectapi", ("/wellness-service/x",), {"params": {"date": "2026-08-01"}})
    ]


def test_download_activity_writes_file(fake, tmp_path):
    fake(download_activity=b"<gpx/>")
    target = tmp_path / "run.gpx"
    result = json.loads(run(server.download_activity("42", "gpx", str(target))))
    assert target.read_bytes() == b"<gpx/>"
    assert result == {"path": str(target), "bytes": 6, "format": "gpx"}


def test_download_activity_into_directory(fake, tmp_path):
    fake(download_activity=b"data")
    result = json.loads(run(server.download_activity("7", "tcx", str(tmp_path))))
    assert result["path"] == str(tmp_path / "activity_7.tcx")


def test_download_activity_rejects_unknown_format(fake):
    fake(download_activity=b"")
    with pytest.raises(ValueError, match="Unsupported format"):
        run(server.download_activity("42", "pdf"))


def test_status_reports_auth_failure(monkeypatch):
    class BrokenSession:
        async def login(self):
            raise GarminError("no tokens")

    monkeypatch.setattr(server, "SESSION", BrokenSession())
    result = json.loads(run(server.garmin_status()))
    assert result["authenticated"] is False
    assert result["error"] == "no tokens"


def test_status_reports_identity(fake):
    fake()
    result = json.loads(run(server.garmin_status()))
    assert result["authenticated"] is True
    assert result["full_name"] == "Test User"


def test_bad_date_becomes_a_readable_tool_error(fake):
    fake(get_user_summary={})
    with pytest.raises(ValueError, match="Could not parse date"):
        run(server.get_daily_summary("someday"))
