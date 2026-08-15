#!/usr/bin/env python
"""Exercise every tool against a real Garmin account and report pass/fail.

Run this on a machine that has already signed in:

    garmin-mcp login
    python scripts/live_check.py

It launches the MCP server exactly as a client would (stdio), calls each tool,
and prints one line per call plus a short preview of the payload — so a failure
points at a specific tool rather than "the server is broken".

Nothing is written to your Garmin account; the only side effect is a GPX file
in the download directory when the activity drill-down runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

CALLS: list[tuple[str, dict]] = [
    ("garmin_status", {}),
    ("get_user_profile", {}),
    ("get_devices", {}),
    ("get_daily_summary", {"date": "yesterday"}),
    ("get_steps", {"start_date": "-7d"}),
    ("get_sleep", {"date": "yesterday"}),
    ("get_heart_rate", {"date": "yesterday"}),
    ("get_resting_heart_rate", {"start_date": "-14d"}),
    ("get_hrv", {"date": "yesterday"}),
    ("get_stress", {"date": "yesterday"}),
    ("get_body_battery", {"start_date": "-3d"}),
    ("get_spo2", {"date": "yesterday"}),
    ("get_respiration", {"date": "yesterday"}),
    ("get_intensity_minutes", {"date": "yesterday"}),
    ("get_floors", {"date": "yesterday"}),
    ("get_hydration", {"date": "yesterday"}),
    ("get_training_readiness", {"date": "yesterday"}),
    ("get_training_status", {"date": "yesterday"}),
    ("get_fitness_metrics", {"date": "yesterday"}),
    ("get_race_predictions", {}),
    ("get_personal_records", {}),
    ("get_heart_rate_zones", {}),
    ("get_progress_summary", {"start_date": "-30d", "metric": "distance"}),
    ("get_goals", {}),
    ("get_workouts", {"limit": 5}),
    ("list_activities", {"limit": 5}),
    ("get_activity_types", {}),
    ("get_body_composition", {"start_date": "-30d"}),
    ("get_weigh_ins", {"start_date": "-30d"}),
    ("get_blood_pressure", {"start_date": "-30d"}),
    ("get_gear", {}),
    ("garmin_api_get", {"path": "/userprofile-service/socialProfile"}),
]

DRILL_DOWN = [
    ("get_activity", {}),
    ("get_activity_splits", {}),
    ("get_activity_weather", {}),
    ("get_activity_details", {"max_points": 20}),
    ("download_activity", {"file_format": "gpx"}),
]


def render(name: str, args: dict, text: str, failed: bool, preview: int) -> None:
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"[{'FAIL' if failed else 'ok  '}] {name}({shown})")
    print(f"        {text[:preview].replace(chr(10), ' ')}")


async def run(preview: int, skip_download: bool) -> int:
    command = shutil.which("garmin-mcp")
    if not command:
        print("garmin-mcp is not on PATH — activate the virtualenv first.", file=sys.stderr)
        return 2

    params = StdioServerParameters(command=command, args=[], env=dict(os.environ))
    failures: list[str] = []
    activity_id: str | None = None

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools
        print(f"{len(tools)} tools registered\n")

        for name, args in CALLS:
            result = await session.call_tool(name, args)
            text = result.content[0].text if result.content else ""
            if result.is_error:
                failures.append(name)
            render(name, args, text, result.is_error, preview)

            if name == "list_activities" and not result.is_error:
                try:
                    items = json.loads(text)
                except json.JSONDecodeError:
                    items = None
                if isinstance(items, list) and items:
                    activity_id = str(items[0].get("activityId"))

        if activity_id:
            print(f"\nDrilling into activity {activity_id}\n")
            for name, extra in DRILL_DOWN:
                if name == "download_activity" and skip_download:
                    continue
                args = {"activity_id": activity_id, **extra}
                result = await session.call_tool(name, args)
                text = result.content[0].text if result.content else ""
                if result.is_error:
                    failures.append(name)
                render(name, args, text, result.is_error, preview)
        else:
            print("\nNo activities found, skipped the activity drill-down.")

    print(f"\n{len(failures)} failing call(s)" + (f": {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preview", type=int, default=220, help="characters of each payload to show"
    )
    parser.add_argument("--skip-download", action="store_true", help="don't write a GPX file")
    args = parser.parse_args()
    return asyncio.run(run(args.preview, args.skip_download))


if __name__ == "__main__":
    raise SystemExit(main())
