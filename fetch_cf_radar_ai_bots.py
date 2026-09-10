#!/usr/bin/env python3
import argparse
import csv
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
if not API_TOKEN:
    sys.exit("Missing CLOUDFLARE_API_TOKEN. Run: export CLOUDFLARE_API_TOKEN='your_token'")

base_url = "https://api.cloudflare.com/client/v4/radar/ai/bots/timeseries"

def previous_month(today):
    """Full range of the calendar month before `today`, as (first_day, last_day)."""
    last_day = today.replace(day=1) - timedelta(days=1)
    return last_day.replace(day=1), last_day

def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")

default_start, default_end = previous_month(date.today())

parser = argparse.ArgumentParser(
    description="Fetch Cloudflare Radar worldwide AI-bot daily timeseries as CSV. "
    "With no arguments it fetches the calendar month that just ended, which is "
    "what the monthly GitHub Actions run does.",
)
parser.add_argument("--start", type=parse_date, default=default_start,
                    help=f"first day to fetch, YYYY-MM-DD (default: {default_start})")
parser.add_argument("--end", type=parse_date, default=default_end,
                    help=f"last day to fetch, YYYY-MM-DD (default: {default_end})")
parser.add_argument("--out-dir", default=".",
                    help="directory to write the CSV into (default: the current directory)")
args = parser.parse_args()

START_DATE = args.start
END_DATE = args.end

if START_DATE > END_DATE:
    sys.exit(f"--start ({START_DATE}) is after --end ({END_DATE})")

def month_chunks(start_date, end_date):
    current = start_date
    while current <= end_date:
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)

        chunk_end = min(end_date, next_month - timedelta(days=1))
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)

def fetch_chunk(chunk_start, chunk_end):
    params = {
        "aggInterval": "1d",
        "dateStart": f"{chunk_start.isoformat()}T00:00:00Z",
        "dateEnd": f"{chunk_end.isoformat()}T23:59:59Z",
        "format": "csv",
        "name": "ai_bots_worldwide",
    }

    url = base_url + "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {API_TOKEN}",
            "Accept": "text/csv",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        sys.exit(
            f"Cloudflare API error {e.code} for {chunk_start} to {chunk_end}:\n{error_body}"
        )
    except Exception as e:
        sys.exit(f"Request failed for {chunk_start} to {chunk_end}: {e}")

    rows = list(csv.reader(body.splitlines()))
    if not rows:
        return [], []

    return rows[0], rows[1:]

all_rows = []
header = None

for chunk_start, chunk_end in month_chunks(START_DATE, END_DATE):
    print(f"Fetching {chunk_start} to {chunk_end}...")
    chunk_header, chunk_rows = fetch_chunk(chunk_start, chunk_end)

    if header is None:
        header = chunk_header

    all_rows.extend(chunk_rows)

# Fail loudly rather than committing an empty file: an empty result means the
# API answered but had nothing for us, which is never the expected outcome.
if not all_rows:
    sys.exit(f"No rows returned for {START_DATE} to {END_DATE}; refusing to write an empty CSV.")

filename = (
    f"cloudflare_radar_ai_bots_worldwide_daily_"
    f"{START_DATE.isoformat()}_to_{END_DATE.isoformat()}.csv"
)
os.makedirs(args.out_dir, exist_ok=True)
output_file = os.path.join(args.out_dir, filename)

with open(output_file, "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(header)
    writer.writerows(all_rows)

print()
print(f"Saved: {output_file}")
print(f"Rows excluding header: {len(all_rows)}")
print()

with open(output_file, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        if i >= 10:
            break
        print(line.rstrip())
