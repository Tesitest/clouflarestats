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
    description="Fetch Cloudflare Radar worldwide AI-bot daily timeseries as CSV. "
    "With no arguments it writes two files: an immutable archive CSV for the "
    "calendar month that just ended, and a rolling window CSV covering the last "
    "N months, overwritten each run. That is what the monthly GitHub Actions run does.",
)
parser.add_argument("--start", type=parse_date,
                    help="first day to fetch, YYYY-MM-DD; use with --end for a one-off "
                         "backfill, which writes only a dated archive file")
parser.add_argument("--end", type=parse_date,
                    help="last day to fetch, YYYY-MM-DD")
parser.add_argument("--rolling-months", type=int, default=6, metavar="N",
                    help="how many trailing months the rolling file covers (default: 6)")
parser.add_argument("--out-dir", default=".",
                    help="directory to write the CSVs into (default: the current directory)")
args = parser.parse_args()

if bool(args.start) != bool(args.end):
    sys.exit("--start and --end must be given together.")
if args.rolling_months < 1:
    sys.exit(f"--rolling-months must be at least 1, got {args.rolling_months}")

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

if args.start:
    if args.start > args.end:
        sys.exit(f"--start ({args.start}) is after --end ({args.end})")
    window_start, window_end = args.start, args.end
else:
    # The rolling window ends with the month that just closed, so the archive
    # month is always the window's final chunk — fetch once, write twice.
    archive_start, window_end = previous_month(date.today())
    window_start = shift_months(archive_start, -(args.rolling_months - 1))

header = None
chunks = []

for chunk_start, chunk_end in month_chunks(window_start, window_end):
    print(f"Fetching {chunk_start} to {chunk_end}...")
    chunk_header, chunk_rows = fetch_chunk(chunk_start, chunk_end)

    if header is None:
        header = chunk_header

    chunks.append((chunk_start, chunk_end, chunk_rows))

print()

if args.start:
    all_rows = [row for _, _, rows in chunks for row in rows]
    written = [write_csv(dated_name(window_start, window_end), header, all_rows)]
else:
    archive_start, archive_end, archive_rows = chunks[-1]
    all_rows = [row for _, _, rows in chunks for row in rows]
    written = [
        write_csv(dated_name(archive_start, archive_end), header, archive_rows),
        write_csv(
            f"cloudflare_radar_ai_bots_worldwide_daily_last_{args.rolling_months}_months.csv",
            header,
            all_rows,
        ),
    ]

print()
print(f"Window covered: {window_start} to {window_end}")
print()

with open(written[-1], "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        if i >= 10:
            break
        print(line.rstrip())
