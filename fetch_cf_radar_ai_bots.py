#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
if not API_TOKEN:
    sys.exit("Missing CLOUDFLARE_API_TOKEN. Run: export CLOUDFLARE_API_TOKEN='your_token'")

base_url = "https://api.cloudflare.com/client/v4/radar/ai/bots/timeseries"

def previous_month(today):
    """Full range of the calendar month before `today`, as (first_day, last_day)."""
    last_day = today.replace(day=1) - timedelta(days=1)
    return last_day.replace(day=1), last_day

def shift_months(first_of_month, delta):
    """Move a first-of-month date by `delta` whole months."""
    total = first_of_month.year * 12 + (first_of_month.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)

def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")

parser = argparse.ArgumentParser(
    description="Fetch the Cloudflare Radar worldwide AI-bot timeseries as CSV. "
    "With no arguments it writes two files, each from a single API request: a "
    "daily archive CSV for the calendar month that just ended, and a weekly "
    "rolling-window CSV covering the last N months, overwritten each run. The "
    "API normalizes per response, so the two files are NOT comparable to each "
    "other. That is what the monthly GitHub Actions run does.",
)
parser.add_argument("--start", type=parse_date,
                    help="first day to fetch, YYYY-MM-DD; use with --end for a one-off "
                         "backfill, which writes only a dated archive file")
parser.add_argument("--end", type=parse_date,
                    help="last day to fetch, YYYY-MM-DD")
parser.add_argument("--rolling-months", type=int, default=6, metavar="N",
                    help="how many trailing months the rolling file covers (default: 6)")
parser.add_argument("--agg", default="1d", choices=["15m", "1h", "1d", "1w"],
                    help="aggregation interval for a --start/--end backfill (default: 1d). "
                         "The API rejects intervals that are too fine for the range.")
parser.add_argument("--out-dir", default=".",
                    help="directory to write the CSVs into (default: the current directory)")
args = parser.parse_args()

if bool(args.start) != bool(args.end):
    sys.exit("--start and --end must be given together.")
if args.rolling_months < 1:
    sys.exit(f"--rolling-months must be at least 1, got {args.rolling_months}")

def request(start_date, end_date, fmt, agg):
    params = {
        "aggInterval": agg,
        "dateStart": f"{start_date.isoformat()}T00:00:00Z",
        "dateEnd": f"{end_date.isoformat()}T23:59:59Z",
        "format": fmt,
        "name": "ai_bots_worldwide",
    }

    url = base_url + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Accept": "text/csv" if fmt == "csv" else "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        sys.exit(
            f"Cloudflare API error {e.code} for {start_date} to {end_date}:\n{error_body}"
        )
    except Exception as e:
        sys.exit(f"Request failed for {start_date} to {end_date}: {e}")

def fetch_series(start_date, end_date, agg):
    print(f"Fetching {start_date} to {end_date} at {agg} in a single request...")
    rows = list(csv.reader(request(start_date, end_date, "csv", agg).splitlines()))
    if not rows:
        return [], []

    return rows[0], rows[1:]

def fetch_meta(start_date, end_date, agg):
    """The API's own description of what the numbers mean.

    Radar normalizes per response, so the scale is a property of the request,
    not of the world. Recording it next to the data keeps anything reading the
    CSV from inventing a unit.
    """
    payload = json.loads(request(start_date, end_date, "json", agg))
    meta = payload.get("result", {}).get("meta", {})
    return {
        "normalization": meta.get("normalization"),
        "aggInterval": meta.get("aggInterval"),
        "dateRange": meta.get("dateRange"),
        "confidenceInfo": meta.get("confidenceInfo"),
        "units": meta.get("units"),
    }

def write_csv(filename, header, rows):
    # Fail loudly rather than committing an empty file: an empty result means the
    # API answered but had nothing for us, which is never the expected outcome.
    if not rows:
        sys.exit(f"No rows to write for {filename}; refusing to write an empty CSV.")

    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, filename)

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

    print(f"Saved: {path}  ({len(rows)} rows excluding header)")
    return path

def dated_name(start_date, end_date):
    return (
        f"cloudflare_radar_ai_bots_worldwide_daily_"
        f"{start_date.isoformat()}_to_{end_date.isoformat()}.csv"
    )

ROLLING_AGG = "1w"
ARCHIVE_AGG = "1d"

if args.start:
    if args.start > args.end:
        sys.exit(f"--start ({args.start}) is after --end ({args.end})")
    window_start, window_end = args.start, args.end
else:
    # The rolling window ends with the month that just closed, so the archive
    # month is the tail of the window — one request, two files.
    archive_start, window_end = previous_month(date.today())
    window_start = shift_months(archive_start, -(args.rolling_months - 1))

# Every file is ONE request. Radar normalizes each response independently, so
# rows from separate requests sit on different scales and must never be
# concatenated or compared: https://developers.cloudflare.com/radar/concepts/normalization/
# That rules out the old month-at-a-time chunking. It also caps resolution --
# the API rejects 1d over a multi-month range ("aggregation interval is too low
# for date range") -- so the rolling window is weekly and the single-month
# archive keeps daily detail. The two files are each internally comparable and
# are NOT comparable to each other.
if args.start:
    header, rows = fetch_series(window_start, window_end, args.agg)
    print()
    written = [write_csv(dated_name(window_start, window_end), header, rows)]
    meta_windows = {"backfill": {"start": window_start.isoformat(),
                                 "end": window_end.isoformat(),
                                 "aggInterval": args.agg,
                                 **fetch_meta(window_start, window_end, args.agg)}}
else:
    archive_header, archive_rows = fetch_series(archive_start, window_end, ARCHIVE_AGG)
    rolling_header, rolling_rows = fetch_series(window_start, window_end, ROLLING_AGG)
    print()
    written = [
        write_csv(dated_name(archive_start, window_end), archive_header, archive_rows),
        write_csv(
            f"cloudflare_radar_ai_bots_worldwide_weekly_last_{args.rolling_months}_months.csv",
            rolling_header,
            rolling_rows,
        ),
    ]
    meta_windows = {
        "archive_month": {"start": archive_start.isoformat(),
                          "end": window_end.isoformat(),
                          "aggInterval": ARCHIVE_AGG,
                          **fetch_meta(archive_start, window_end, ARCHIVE_AGG)},
        "rolling_window": {"start": window_start.isoformat(),
                           "end": window_end.isoformat(),
                           "aggInterval": ROLLING_AGG,
                           **fetch_meta(window_start, window_end, ROLLING_AGG)},
    }

meta_path = os.path.join(args.out_dir, "radar_meta.json")
with open(meta_path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "Each series is normalized independently by the API. "
                    "Values from different series are not comparable.",
            "series": meta_windows,
        },
        f, indent=2, sort_keys=True,
    )
    f.write("\n")
print(f"Saved: {meta_path}")

print()
print(f"Window covered: {window_start} to {window_end}")
print()

with open(written[-1], "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        if i >= 10:
            break
        print(line.rstrip())
