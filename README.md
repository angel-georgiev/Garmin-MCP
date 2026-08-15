# Garmin MCP

An [MCP](https://modelcontextprotocol.io) server that puts your Garmin Connect data —
sleep, HRV, stress, body battery, training readiness, activities, weight, gear — in front
of any MCP client (Claude Code, Claude Desktop, and others).

It wraps [`garminconnect`](https://github.com/cyberjunky/python-garminconnect), which talks
to the same private API the Garmin Connect website uses. There is no official public API;
your credentials stay on your machine and tokens are cached locally.

## Install

Requires Python 3.12+.

```bash
git clone https://github.com/angel-georgiev/Garmin-MCP.git
cd Garmin-MCP
uv venv && uv pip install -e .
```

`pip install -e .` inside a virtualenv works just as well.

## Sign in once

Garmin accounts usually have MFA enabled, and an MCP server talking over stdio cannot
prompt you for a code. So log in from a terminal first — the OAuth tokens get cached in
`~/.garminconnect` and the server reuses them (they last about a year).

```bash
garmin-mcp login          # prompts for email, password and MFA code
garmin-mcp status         # verify the cached session works
```

You can pre-seed the email and password with `GARMIN_EMAIL` / `GARMIN_PASSWORD` (in the
environment or a `.env` file — see `.env.example`). Once tokens are cached, the password
is no longer needed.

## Add it to a client

**Claude Code**

```bash
claude mcp add garmin -- /absolute/path/to/Garmin-MCP/.venv/bin/garmin-mcp
```

**Claude Desktop** — in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/absolute/path/to/Garmin-MCP/.venv/bin/garmin-mcp",
      "env": {
        "GARMINTOKENS": "/Users/you/.garminconnect"
      }
    }
  }
}
```

Use absolute paths: MCP clients do not inherit your shell's `PATH` or working directory.

Then ask things like *"how did I sleep last week?"*, *"compare my running pace this month
to last month"*, or *"what's my training readiness today and why is it low?"*.

## Use it as a Claude connector (remote MCP)

Local stdio only reaches Claude Code and Claude Desktop. To use it from claude.ai or the
mobile app you need a **custom connector**, which requires a public HTTPS URL that
Anthropic's servers can reach — `localhost` is not accepted.

The catch: **don't deploy this to a VPS or cloud host.** Garmin's login refuses datacenter
IPs (see [Troubleshooting](#troubleshooting)), so a hosted copy can't sign in at all. Run
it on your own machine and expose that through a tunnel, so the traffic to Garmin leaves
from your home connection:

```bash
garmin-mcp login                      # once, so tokens are cached
garmin-mcp serve --transport streamable-http --port 8000 --path /mcp-$(openssl rand -hex 8)
cloudflared tunnel --url http://127.0.0.1:8000    # in a second terminal
```

`cloudflared` prints a `https://<random>.trycloudflare.com` hostname. The connector URL is
that hostname plus the path you generated:

```
https://<random>.trycloudflare.com/mcp-<your-random-hex>
```

Paste it into **Settings → Connectors → Add custom connector** on claude.ai. Leave the
OAuth fields empty — this server doesn't implement OAuth.

<!-- markdownlint-disable-next-line -->
> **This endpoint is unauthenticated.** Anyone who learns the URL can read every health
> metric in your Garmin account. The random path makes it hard to guess, but that is
> obscurity, not security — a proxy or tunnel log leaks it permanently. Treat the URL as a
> password, prefer a tunnel that enforces its own access control, and tear the tunnel down
> when you're not using it. Both the server and the tunnel must stay running for the
> connector to work, so this doesn't survive closing your laptop.

## Tools

| Area | Tools |
| --- | --- |
| Account | `garmin_status`, `get_user_profile`, `get_devices` |
| Daily health | `get_daily_summary`, `get_steps`, `get_floors`, `get_intensity_minutes`, `get_hydration` |
| Heart & recovery | `get_heart_rate`, `get_resting_heart_rate`, `get_hrv`, `get_stress`, `get_body_battery`, `get_spo2`, `get_respiration` |
| Sleep | `get_sleep` |
| Training | `get_training_readiness`, `get_training_status`, `get_fitness_metrics`, `get_race_predictions`, `get_personal_records`, `get_heart_rate_zones`, `get_progress_summary`, `get_goals`, `get_workouts` |
| Activities | `list_activities`, `get_activity`, `get_activity_details`, `get_activity_splits`, `get_activity_weather`, `get_activity_exercise_sets`, `get_activity_types`, `download_activity` |
| Body | `get_body_composition`, `get_weigh_ins`, `get_blood_pressure` |
| Gear | `get_gear` |
| Escape hatch | `garmin_api_get` |

Every tool is read-only. Nothing writes to your Garmin account, and `garmin_api_get` only
issues GETs.

Notes on behaviour:

- **Dates are lenient.** `2026-08-01`, `today`, `yesterday`, `-7d`, `30 days ago` all work.
  Omit them and you get today (or the last 7–30 days for range tools).
- **Responses are trimmed.** Garmin returns per-second time series that would swamp a
  context window, so detail-heavy tools default to summaries and take
  `include_detail: true` when you really want the raw arrays. Anything still too large is
  truncated with a `__truncated__` marker rather than silently cut. Raise the ceiling with
  `GARMIN_MCP_MAX_CHARS`.
- **Empty responses are normal.** A metric only exists if your watch recorded it that day.
- **`download_activity`** writes GPX/TCX/CSV/FIT to disk and returns the path.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GARMIN_EMAIL` | – | Account email (only needed until tokens are cached) |
| `GARMIN_PASSWORD` | – | Account password (same) |
| `GARMINTOKENS` | `~/.garminconnect` | Where OAuth tokens are cached |
| `GARMIN_IS_CN` | `0` | Set to `1` for Garmin China (`garmin.cn`) accounts |
| `GARMIN_MCP_MAX_CHARS` | `40000` | Max characters of JSON per tool response |
| `GARMIN_MCP_DOWNLOAD_DIR` | `~/garmin-downloads` | Default target for `download_activity` |

`.env` in the working directory and `~/.garmin-mcp.env` are loaded automatically; real
environment variables always win.

## Troubleshooting

- **Run it on your own machine, not a cloud VM.** Garmin fronts its login with Cloudflare
  and rate limits by IP. From a datacenter address the login fails before it ever checks
  your password — `429 IP rate limited` on the mobile endpoint, `403 Cloudflare bot
  challenge` on the portal endpoint. Residential connections are fine; CI runners, cloud
  dev containers and VPS hosts generally are not.
- **"MFA code required"** — the server cannot prompt. Run `garmin-mcp login` in a terminal.
- **429 / rate limited** — Garmin throttles aggressively and repeated logins can lock an
  account temporarily. Wait several minutes; don't loop retries.
- **Auth suddenly fails** — tokens expire or get invalidated after a password change. Run
  `garmin-mcp logout && garmin-mcp login`.
- **A metric returns 404** — that endpoint isn't available for your device or subscription.

## Development

```bash
uv pip install -e ".[dev]"
pytest          # 42 tests, no network or credentials needed
ruff check .
```

Tests drive the tools against a fake Garmin client, so the suite runs offline.

To check the whole thing against your real account once you're signed in:

```bash
python scripts/live_check.py
```

It starts the server over stdio like a real client, calls every tool, drills into your
most recent activity, and prints a pass/fail line per call. `--skip-download` avoids
writing a GPX file; `--preview 0` hides the payload previews if you don't want your data
on screen.

## Privacy

Health data is about as personal as it gets. This server runs locally, sends your
credentials only to Garmin's SSO endpoint, and stores tokens on your own disk. Be aware
that anything a tool returns is sent to whatever model your MCP client is talking to.

## License

MIT — see [LICENSE](LICENSE).
