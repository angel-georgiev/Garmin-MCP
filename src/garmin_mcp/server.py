"""MCP server exposing Garmin Connect data as tools."""

from __future__ import annotations

import functools
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from garminconnect import Garmin

from . import __version__
from .config import Settings
from .dates import parse_date, parse_range
from .formatting import drop_keys, pick, to_json_text
from .session import GarminError, GarminSession

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _ServerClass
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore[assignment]

logger = logging.getLogger("garmin_mcp")

INSTRUCTIONS = """\
Read-only access to the signed-in user's Garmin Connect account: daily health
metrics, sleep, HRV, stress, body battery, training readiness/status, activities,
body composition and gear.

Guidance:
- Dates accept 'YYYY-MM-DD', 'today', 'yesterday', offsets like '-7d', or '30 days ago'.
- Prefer the summary tools (get_daily_summary, list_activities) before pulling
  detailed time series — detail responses are large.
- Metrics only exist if the user's watch and subscription record them; an empty
  response usually means "not tracked that day", not an error.
- garmin_api_get is an escape hatch for endpoints without a dedicated tool.
"""

SETTINGS = Settings.from_env()
SESSION = GarminSession(SETTINGS)


def _build_server() -> tuple[Any, Any]:
    """Build the MCP server, with OAuth in front of it when configured.

    Without a passphrase and public URL the server is stdio-shaped: local,
    single-client, no auth needed. With both set it becomes a connector-shaped
    server and every request must carry a bearer token.
    """
    if not SETTINGS.oauth_enabled:
        return _ServerClass(name="garmin", version=__version__, instructions=INSTRUCTIONS), None

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    from .auth import SCOPE, SingleUserOAuthProvider

    provider = SingleUserOAuthProvider(
        passphrase=SETTINGS.auth_passphrase or "",
        base_url=SETTINGS.public_url or "",
        storage=SETTINGS.oauth_state_file,
    )
    server = _ServerClass(
        name="garmin",
        version=__version__,
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=SETTINGS.public_url,
            resource_server_url=SETTINGS.public_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[SCOPE],
                default_scopes=[SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )
    return server, provider


mcp, OAUTH_PROVIDER = _build_server()

if OAUTH_PROVIDER is not None:
    from .auth import register_login_route

    register_login_route(mcp, OAUTH_PROVIDER)

# Heavy per-second time series stripped from sleep responses unless asked for.
SLEEP_DETAIL_KEYS = {
    "sleepLevels",
    "sleepMovement",
    "sleepHeartRate",
    "sleepStress",
    "sleepBodyBattery",
    "sleepRestlessMoments",
    "wellnessEpochRespirationDataDTOList",
    "wellnessEpochSPO2DataDTOList",
    "breathingDisruptionData",
    "hrvData",
}

ACTIVITY_SUMMARY_KEYS = [
    "activityId",
    "activityName",
    "startTimeLocal",
    "distance",
    "duration",
    "elapsedDuration",
    "movingDuration",
    "elevationGain",
    "elevationLoss",
    "averageSpeed",
    "maxSpeed",
    "averageHR",
    "maxHR",
    "calories",
    "steps",
    "averageRunningCadenceInStepsPerMinute",
    "avgPower",
    "normPower",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "vO2MaxValue",
    "locationName",
]


def _text(data: Any) -> str:
    return to_json_text(data, SETTINGS.max_response_chars)


def tool(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Register a coroutine as an MCP tool, mapping errors to clean messages."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except GarminError as exc:
            # MCP surfaces this as an error result the model can read and act on.
            raise ValueError(str(exc)) from exc

    return mcp.tool()(wrapper)


def _summarize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    summary = pick(activity, ACTIVITY_SUMMARY_KEYS)
    activity_type = activity.get("activityType")
    if isinstance(activity_type, dict):
        summary["activityType"] = activity_type.get("typeKey")
    event_type = activity.get("eventType")
    if isinstance(event_type, dict):
        summary["eventType"] = event_type.get("typeKey")
    return summary


def _profile_number(client: Garmin) -> str:
    """Best-effort lookup of the numeric profile id that gear endpoints need."""
    profile = client.get_user_profile()
    if isinstance(profile, dict):
        for key in ("userProfileNumber", "profileId", "id"):
            value = profile.get(key)
            if value:
                return str(value)
        user_data = profile.get("userData")
        if isinstance(user_data, dict) and user_data.get("userProfilePk"):
            return str(user_data["userProfilePk"])
    settings = client.get_userprofile_settings()
    if isinstance(settings, dict):
        for key in ("userProfileNumber", "profileId", "id"):
            value = settings.get(key)
            if value:
                return str(value)
    raise GarminError("Could not determine the Garmin profile number for gear lookups.")


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------


@tool
async def garmin_status() -> str:
    """Check the Garmin Connect connection: who is signed in, and how.

    Call this first when something fails — it distinguishes an auth problem
    from a data problem.
    """
    info: dict[str, Any] = {
        "tokenstore": str(SETTINGS.tokenstore),
        "cached_tokens_present": SETTINGS.has_cached_tokens,
        "credentials_in_env": SETTINGS.has_credentials,
        "domain": "garmin.cn" if SETTINGS.is_cn else "garmin.com",
    }
    try:
        client = await SESSION.login()
    except GarminError as exc:
        info["authenticated"] = False
        info["error"] = str(exc)
        return _text(info)

    info["authenticated"] = True
    info["full_name"] = client.full_name
    info["display_name"] = client.display_name
    info["unit_system"] = client.unit_system
    return _text(info)


@tool
async def get_user_profile() -> str:
    """Get the account profile and measurement settings (units, timezone, birth date)."""
    data = await SESSION.call(lambda c: c.get_user_profile())
    return _text(data)


@tool
async def get_devices(include_settings: bool = False) -> str:
    """List registered Garmin devices, optionally with each device's settings.

    Args:
        include_settings: Also fetch per-device settings (much larger response).
    """

    def fetch(client: Garmin) -> Any:
        devices = client.get_devices()
        if not include_settings:
            return devices
        enriched = []
        for device in devices or []:
            entry = dict(device)
            device_id = device.get("deviceId")
            if device_id:
                try:
                    entry["settings"] = client.get_device_settings(device_id)
                except Exception as exc:  # pragma: no cover - optional enrichment
                    entry["settings_error"] = str(exc)
            enriched.append(entry)
        return enriched

    return _text(await SESSION.call(fetch))


# --------------------------------------------------------------------------
# Daily health
# --------------------------------------------------------------------------


@tool
async def get_daily_summary(date: str | None = None) -> str:
    """Get the all-round daily wellness summary for one day.

    Steps, distance, floors, calories, intensity minutes, resting heart rate,
    stress averages and body battery range — the best starting point for
    "how was my day".

    Args:
        date: Day to fetch. Defaults to today.
    """
    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_user_summary(cdate))
    return _text(data)


@tool
async def get_steps(
    start_date: str | None = None,
    end_date: str | None = None,
    daily_detail: bool = False,
) -> str:
    """Get step counts per day, or the intraday step buckets for a single day.

    Args:
        start_date: First day. Defaults to 7 days before end_date.
        end_date: Last day. Defaults to today.
        daily_detail: Return 15-minute step buckets for start_date instead of daily totals.
    """
    start, end = parse_range(start_date, end_date)
    if daily_detail:
        data = await SESSION.call(lambda c: c.get_steps_data(start))
        return _text({"date": start, "intraday": data})
    data = await SESSION.call(lambda c: c.get_daily_steps(start, end))
    return _text(data)


@tool
async def get_sleep(date: str | None = None, include_detail: bool = False) -> str:
    """Get sleep for one night: stages, duration, sleep score, SpO2 and respiration.

    Args:
        date: The day the sleep is recorded against (the morning you woke up). Defaults to today.
        include_detail: Include per-minute stage/movement/heart-rate series. Large.
    """
    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_sleep_data(cdate))
    if not include_detail:
        data = drop_keys(data, SLEEP_DETAIL_KEYS)
    return _text(data)


@tool
async def get_heart_rate(date: str | None = None, include_detail: bool = False) -> str:
    """Get heart rate for one day: resting, min/max, and optionally the intraday series.

    Args:
        date: Day to fetch. Defaults to today.
        include_detail: Include the raw 2-minute heart-rate samples. Large.
    """
    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_heart_rates(cdate))
    if not include_detail:
        data = drop_keys(data, {"heartRateValues", "heartRateValueDescriptors"})
    return _text(data)


@tool
async def get_resting_heart_rate(start_date: str | None = None, end_date: str | None = None) -> str:
    """Get resting heart rate per day over a range.

    Args:
        start_date: First day. Defaults to 7 days before end_date.
        end_date: Last day. Defaults to today.
    """
    start, end = parse_range(start_date, end_date)
    if start == end:
        data = await SESSION.call(lambda c: c.get_rhr_day(start))
    else:
        data = await SESSION.call(lambda c: c.get_rhr_daily(start, end))
    return _text(data)


@tool
async def get_hrv(
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_detail: bool = False,
) -> str:
    """Get heart rate variability: overnight HRV status, baseline and weekly average.

    Args:
        date: Single day to fetch. Defaults to today when no range is given.
        start_date: First day of a range (overrides `date`).
        end_date: Last day of a range.
        include_detail: Include the 5-minute HRV readings for a single day.
    """
    if start_date or end_date:
        start, end = parse_range(start_date, end_date)
        data = await SESSION.call(lambda c: c.get_hrv_data_range(start, end))
        return _text(data)

    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_hrv_data(cdate))
    if not include_detail:
        data = drop_keys(data, {"hrvReadings"})
    return _text(data)


@tool
async def get_stress(date: str | None = None, include_detail: bool = False) -> str:
    """Get stress levels for one day (average, max and time in each stress band).

    Args:
        date: Day to fetch. Defaults to today.
        include_detail: Include the 3-minute stress samples. Large.
    """
    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_stress_data(cdate))
    if not include_detail:
        data = drop_keys(data, {"stressValuesArray", "bodyBatteryValuesArray"})
    return _text(data)


@tool
async def get_body_battery(
    start_date: str | None = None,
    end_date: str | None = None,
    include_detail: bool = False,
) -> str:
    """Get body battery (energy) levels over a range, with charge/drain events.

    Args:
        start_date: First day. Defaults to 7 days before end_date.
        end_date: Last day. Defaults to today.
        include_detail: Include the per-reading arrays. Large.
    """
    start, end = parse_range(start_date, end_date)
    data = await SESSION.call(lambda c: c.get_body_battery(start, end))
    if not include_detail:
        data = drop_keys(data, {"bodyBatteryValuesArray", "bodyBatteryValueDescriptorDTOList"})
    return _text(data)


@tool
async def get_spo2(date: str | None = None, include_detail: bool = False) -> str:
    """Get pulse oximetry (blood oxygen) readings for one day.

    Args:
        date: Day to fetch. Defaults to today.
        include_detail: Include every reading rather than the daily summary.
    """
    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_spo2_data(cdate))
    if not include_detail:
        data = drop_keys(
            data,
            {"spO2HourlyAverages", "spO2SingleValues", "continuousReadingDTOList"},
        )
    return _text(data)


@tool
async def get_respiration(date: str | None = None, include_detail: bool = False) -> str:
    """Get breathing rate for one day (average, min/max, sleep vs waking).

    Args:
        date: Day to fetch. Defaults to today.
        include_detail: Include the raw respiration samples. Large.
    """
    cdate = parse_date(date)
    data = await SESSION.call(lambda c: c.get_respiration_data(cdate))
    if not include_detail:
        data = drop_keys(data, {"respirationValuesArray", "respirationValueDescriptorsDTOList"})
    return _text(data)


@tool
async def get_intensity_minutes(
    date: str | None = None,
    weekly: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get moderate/vigorous intensity minutes for a day, or weekly rollups.

    Args:
        date: Day to fetch when `weekly` is false. Defaults to today.
        weekly: Return weekly totals over start_date..end_date instead of one day.
        start_date: First day of the weekly range. Defaults to 28 days before end_date.
        end_date: Last day of the weekly range. Defaults to today.
    """
    if weekly:
        start, end = parse_range(start_date, end_date, default_days=28)
        data = await SESSION.call(lambda c: c.get_weekly_intensity_minutes(start, end))
    else:
        cdate = parse_date(date)
        data = await SESSION.call(lambda c: c.get_intensity_minutes_data(cdate))
    return _text(data)


@tool
async def get_floors(date: str | None = None) -> str:
    """Get floors climbed and descended for one day.

    Args:
        date: Day to fetch. Defaults to today.
    """
    cdate = parse_date(date)
    return _text(await SESSION.call(lambda c: c.get_floors(cdate)))


@tool
async def get_hydration(date: str | None = None) -> str:
    """Get logged hydration (fluid intake) for one day.

    Args:
        date: Day to fetch. Defaults to today.
    """
    cdate = parse_date(date)
    return _text(await SESSION.call(lambda c: c.get_hydration_data(cdate)))


# --------------------------------------------------------------------------
# Training
# --------------------------------------------------------------------------


@tool
async def get_training_readiness(date: str | None = None) -> str:
    """Get the training readiness score and its inputs (sleep, recovery, HRV, stress).

    Args:
        date: Day to fetch. Defaults to today.
    """
    cdate = parse_date(date)
    return _text(await SESSION.call(lambda c: c.get_training_readiness(cdate)))


@tool
async def get_training_status(date: str | None = None) -> str:
    """Get training status: acute/chronic load, load balance, VO2 max and recovery time.

    Args:
        date: Day to fetch. Defaults to today.
    """
    cdate = parse_date(date)
    return _text(await SESSION.call(lambda c: c.get_training_status(cdate)))


@tool
async def get_fitness_metrics(date: str | None = None) -> str:
    """Get VO2 max, fitness age, endurance score and hill score for one day.

    Args:
        date: Day to fetch. Defaults to today.
    """
    cdate = parse_date(date)

    def fetch(client: Garmin) -> dict[str, Any]:
        result: dict[str, Any] = {"date": cdate}
        for label, call in (
            ("max_metrics", lambda: client.get_max_metrics(cdate)),
            ("fitness_age", lambda: client.get_fitnessage_data(cdate)),
            ("endurance_score", lambda: client.get_endurance_score(cdate)),
            ("hill_score", lambda: client.get_hill_score(cdate)),
        ):
            try:
                result[label] = call()
            except Exception as exc:
                result[label] = {"unavailable": str(exc)}
        return result

    return _text(await SESSION.call(fetch))


@tool
async def get_race_predictions(start_date: str | None = None, end_date: str | None = None) -> str:
    """Get Garmin's predicted race times (5K, 10K, half, marathon).

    Args:
        start_date: First day of a trend range. Omit for the latest prediction.
        end_date: Last day of a trend range.
    """
    if start_date or end_date:
        start, end = parse_range(start_date, end_date, default_days=30)
        return _text(await SESSION.call(lambda c: c.get_race_predictions(start, end)))
    return _text(await SESSION.call(lambda c: c.get_race_predictions()))


@tool
async def get_personal_records() -> str:
    """Get personal records (fastest 1K/5K/10K, longest run, biggest climb, and so on)."""
    return _text(await SESSION.call(lambda c: c.get_personal_record()))


@tool
async def get_heart_rate_zones() -> str:
    """Get the configured heart rate zones and thresholds for the account."""
    return _text(await SESSION.call(lambda c: c.get_heart_rate_zones()))


@tool
async def get_progress_summary(
    start_date: str | None = None,
    end_date: str | None = None,
    metric: str = "distance",
    group_by_activity: bool = True,
) -> str:
    """Get totals for a period, optionally split per activity type.

    Args:
        start_date: First day. Defaults to 30 days before end_date.
        end_date: Last day. Defaults to today.
        metric: One of distance, duration, elevationGain, movingDuration, calories.
        group_by_activity: Break the totals down by activity type.
    """
    start, end = parse_range(start_date, end_date, default_days=30)
    data = await SESSION.call(
        lambda c: c.get_progress_summary_between_dates(start, end, metric, group_by_activity)
    )
    return _text(data)


@tool
async def get_goals(status: str = "active", limit: int = 30) -> str:
    """Get step/distance/activity goals.

    Args:
        status: One of active, future, past.
        limit: Maximum number of goals to return.
    """
    return _text(await SESSION.call(lambda c: c.get_goals(status=status, start=0, limit=limit)))


@tool
async def get_workouts(limit: int = 20) -> str:
    """List saved structured workouts.

    Args:
        limit: Maximum number of workouts to return.
    """
    return _text(await SESSION.call(lambda c: c.get_workouts(0, limit)))


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------


@tool
async def list_activities(
    limit: int = 20,
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
    offset: int = 0,
    full_detail: bool = False,
) -> str:
    """List recorded activities, newest first, as compact summaries.

    Args:
        limit: Maximum activities to return (ignored when a date range is given).
        start_date: Only activities on/after this day. Requires no offset.
        end_date: Only activities on/before this day. Defaults to today when start_date is set.
        activity_type: Garmin type key, e.g. running, cycling, swimming, strength_training.
        offset: Skip this many activities (paging through the newest-first list).
        full_detail: Return every field Garmin sends instead of the summary fields.
    """
    if start_date:
        start, end = parse_range(start_date, end_date, default_days=30, max_days=1830)
        data = await SESSION.call(
            lambda c: c.get_activities_by_date(start, end, activity_type or None)
        )
    else:
        data = await SESSION.call(
            lambda c: c.get_activities(offset, limit, activity_type or None)
        )

    if isinstance(data, list) and not full_detail:
        data = [_summarize_activity(item) for item in data if isinstance(item, dict)]
    return _text(data)


@tool
async def get_activity(activity_id: str) -> str:
    """Get the full summary of one activity (splits and streams are separate tools).

    Args:
        activity_id: Numeric activity id from list_activities.
    """
    return _text(await SESSION.call(lambda c: c.get_activity(activity_id)))


@tool
async def get_activity_details(activity_id: str, max_points: int = 200) -> str:
    """Get the time-series streams of an activity (heart rate, pace, power, elevation, GPS).

    Args:
        activity_id: Numeric activity id.
        max_points: Downsample target for the chart series. Keep small; 2000 is Garmin's max.
    """
    points = max(1, min(max_points, 2000))
    data = await SESSION.call(
        lambda c: c.get_activity_details(activity_id, maxchart=points, maxpoly=points)
    )
    return _text(data)


@tool
async def get_activity_splits(activity_id: str, typed: bool = False) -> str:
    """Get laps/splits for an activity.

    Args:
        activity_id: Numeric activity id.
        typed: Return typed splits (intervals, rest, climbs) instead of plain laps.
    """
    if typed:
        return _text(await SESSION.call(lambda c: c.get_activity_typed_splits(activity_id)))
    return _text(await SESSION.call(lambda c: c.get_activity_splits(activity_id)))


@tool
async def get_activity_weather(activity_id: str) -> str:
    """Get the weather recorded during an activity.

    Args:
        activity_id: Numeric activity id.
    """
    return _text(await SESSION.call(lambda c: c.get_activity_weather(activity_id)))


@tool
async def get_activity_exercise_sets(activity_id: str) -> str:
    """Get strength-training sets (exercise, reps, weight) for an activity.

    Args:
        activity_id: Numeric activity id of a strength/gym activity.
    """
    return _text(await SESSION.call(lambda c: c.get_activity_exercise_sets(activity_id)))


@tool
async def get_activity_types() -> str:
    """List the activity type keys Garmin accepts for filtering."""
    return _text(await SESSION.call(lambda c: c.get_activity_types()))


@tool
async def download_activity(
    activity_id: str,
    file_format: str = "gpx",
    output_path: str | None = None,
) -> str:
    """Download an activity file to disk and return the saved path.

    Args:
        activity_id: Numeric activity id.
        file_format: One of gpx, tcx, csv, original (original is the FIT file, zipped).
        output_path: Where to write. Defaults to GARMIN_MCP_DOWNLOAD_DIR.
    """
    fmt = file_format.strip().lower()
    formats = {
        "gpx": (Garmin.ActivityDownloadFormat.GPX, "gpx"),
        "tcx": (Garmin.ActivityDownloadFormat.TCX, "tcx"),
        "csv": (Garmin.ActivityDownloadFormat.CSV, "csv"),
        "kml": (Garmin.ActivityDownloadFormat.KML, "kml"),
        "original": (Garmin.ActivityDownloadFormat.ORIGINAL, "zip"),
        "fit": (Garmin.ActivityDownloadFormat.ORIGINAL, "zip"),
    }
    if fmt not in formats:
        raise GarminError(f"Unsupported format {file_format!r}. Use one of: {', '.join(formats)}.")

    dl_format, suffix = formats[fmt]
    if output_path:
        target = Path(output_path).expanduser()
        if target.is_dir():
            target = target / f"activity_{activity_id}.{suffix}"
    else:
        target = SETTINGS.download_dir / f"activity_{activity_id}.{suffix}"

    payload = await SESSION.call(lambda c: c.download_activity(activity_id, dl_fmt=dl_format))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return _text({"path": str(target), "bytes": len(payload), "format": fmt})


# --------------------------------------------------------------------------
# Body, gear, everything else
# --------------------------------------------------------------------------


@tool
async def get_body_composition(start_date: str | None = None, end_date: str | None = None) -> str:
    """Get weight and body composition (fat %, muscle, bone, water) over a range.

    Args:
        start_date: First day. Defaults to 30 days before end_date.
        end_date: Last day. Defaults to today.
    """
    start, end = parse_range(start_date, end_date, default_days=30)
    return _text(await SESSION.call(lambda c: c.get_body_composition(start, end)))


@tool
async def get_weigh_ins(start_date: str | None = None, end_date: str | None = None) -> str:
    """Get individual weigh-in entries over a range.

    Args:
        start_date: First day. Defaults to 30 days before end_date.
        end_date: Last day. Defaults to today.
    """
    start, end = parse_range(start_date, end_date, default_days=30)
    return _text(await SESSION.call(lambda c: c.get_weigh_ins(start, end)))


@tool
async def get_blood_pressure(start_date: str | None = None, end_date: str | None = None) -> str:
    """Get logged blood pressure readings over a range.

    Args:
        start_date: First day. Defaults to 30 days before end_date.
        end_date: Last day. Defaults to today.
    """
    start, end = parse_range(start_date, end_date, default_days=30)
    return _text(await SESSION.call(lambda c: c.get_blood_pressure(start, end)))


@tool
async def get_gear(include_stats: bool = False) -> str:
    """List gear (shoes, bikes) registered on the account.

    Args:
        include_stats: Also fetch mileage/usage stats for each item.
    """

    def fetch(client: Garmin) -> Any:
        profile_number = _profile_number(client)
        gear = client.get_gear(profile_number)
        if not include_stats or not isinstance(gear, list):
            return gear
        enriched = []
        for item in gear:
            entry = dict(item)
            uuid = item.get("uuid")
            if uuid:
                try:
                    entry["stats"] = client.get_gear_stats(uuid)
                except Exception as exc:  # pragma: no cover - optional enrichment
                    entry["stats_error"] = str(exc)
            enriched.append(entry)
        return enriched

    return _text(await SESSION.call(fetch))


@tool
async def garmin_api_get(path: str, params: dict[str, Any] | None = None) -> str:
    """Call any Garmin Connect API endpoint directly (read-only escape hatch).

    Use when no dedicated tool covers what you need. Paths are relative to the
    Connect API root, e.g. '/usersummary-service/usersummary/daily/{displayName}'.

    Args:
        path: API path starting with '/'.
        params: Optional query parameters.
    """
    cleaned = path.strip()
    if "://" in cleaned:
        raise GarminError("Pass an API path, not a full URL.")
    if not cleaned.startswith("/"):
        raise GarminError("path must start with '/' — it is relative to the Connect API root.")

    kwargs: dict[str, Any] = {"params": params} if params else {}
    return _text(await SESSION.call(lambda c: c.connectapi(cleaned, **kwargs)))


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp",
) -> None:
    """Start the MCP server.

    ``host``/``port``/``path`` apply only to the HTTP transports. Binding stays
    on loopback by default: exposing this server means exposing every health
    metric in the account, so putting a tunnel or reverse proxy in front should
    be a deliberate act, not the default.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "streamable-http":
        mcp.run(transport=transport, host=host, port=port, streamable_http_path=path)
    else:
        mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    run()
