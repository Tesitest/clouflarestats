#!/usr/bin/env python3
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

START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 6, 28)

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

output_file = "cloudflare_radar_ai_bots_worldwide_daily_2026-01-01_to_2026-06-28.csv"

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
