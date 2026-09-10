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
    "The window is requested as one series per month in a single API call, so "
    "every month shares one normalization maximum and the months can be "
    "compared to each other. With no arguments it writes a daily archive CSV "
    "for the calendar month that just ended plus a daily rolling-window CSV "
    "covering the last N months, both cut from that one response. That is what "
    "the monthly GitHub Actions run does.",
)
parser.add_argument("--start", type=parse_date,
                    help="first day to fetch, YYYY-MM-DD; use with --end for a one-off "
                         "backfill, which writes only a dated archive file")
parser.add_argument("--end", type=parse_date,
                    help="last day to fetch, YYYY-MM-DD")
parser.add_argument("--rolling-months", type=int, default=6, metavar="N",
                    help="how many trailing months the rolling file covers (default: 6)")
parser.add_argument("--agg", default="1d", choices=["15m", "1h", "1d", "1w"],
                    help="aggregation interval, applied to every series in the request "
                         "(default: 1d). The API rejects intervals too fine for a "
                         "single series' date range.")
parser.add_argument("--out-dir", default=".",
                    help="directory to write the CSVs into (default: the current directory)")
args = parser.parse_args()

if bool(args.start) != bool(args.end):
    sys.exit("--start and --end must be given together.")
if args.rolling_months < 1:
    sys.exit(f"--rolling-months must be at least 1, got {args.rolling_months}")

def request(spans, fmt, agg):
    """One HTTP call carrying one series per (label, start, end) in `spans`.

    Radar builds its series from the *position* of each repeated name /
    dateStart / dateEnd, and normalizes every series in a response against a
    single maximum. Asking for all the months at once is therefore the only way
    to get values that can be compared between them; aggInterval and format are
    global to the request.
    https://developers.cloudflare.com/radar/get-started/making-comparisons/
    """
    params = [("aggInterval", agg), ("format", fmt)]
    for label, start_date, end_date in spans:
        params.append(("name", label))
        params.append(("dateStart", f"{start_date.isoformat()}T00:00:00Z"))
        params.append(("dateEnd", f"{end_date.isoformat()}T23:59:59Z"))

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
        span_text = ", ".join(f"{lbl} {s}..{e_}" for lbl, s, e_ in spans)
        sys.exit(f"Cloudflare API error {e.code} for [{span_text}]:\n{error_body}")
    except Exception as e:
        span_text = ", ".join(f"{lbl} {s}..{e_}" for lbl, s, e_ in spans)
        sys.exit(f"Request failed for [{span_text}]: {e}")

CHUNK_DAYS = 28

def equal_spans(window_start, window_end, chunk_days=CHUNK_DAYS):
    """Tile the window with contiguous spans of identical length.

    Radar refuses a multi-series request whose series differ in length
    ("comparisons must have same duration as main series"), so calendar months
    cannot be the unit -- they are 28 to 31 days. Fixed-length chunks tile the
    window instead, walking back from the end; the earliest chunk may reach
    before window_start, and those extra days are trimmed from the result.
    """
    spans = []
    end = window_end
    while end >= window_start:
        start = end - timedelta(days=chunk_days - 1)
        spans.append((f"s{len(spans)}", start, end))
        end = start - timedelta(days=1)
    spans.reverse()
    for i, (_, start, end) in enumerate(spans):
        spans[i] = (f"s{i}", start, end)
    return spans

def fetch_multi(spans, agg):
    """Every month in one response, so all of them share one normalization.

    The CSV comes back as paired columns per series -- "<name> timestamps",
    "<name> values" -- which flatten into one continuous series because they
    were normalized together.
    """
    print(f"Fetching {len(spans)} parallel series at {agg} in a single request "
          f"({spans[0][1]} to {spans[-1][2]})...")
    table = list(csv.reader(request(spans, "csv", agg).splitlines()))
    if not table:
        sys.exit("Empty response from the API.")

    header, body = table[0], table[1:]
    if len(header) < 2 or len(header) % 2:
        sys.exit(f"Unexpected CSV shape; header was: {header}")

    points = []
    for col in range(0, len(header), 2):
        for row in body:
            if len(row) <= col + 1:
                continue
            ts, value = row[col].strip(), row[col + 1].strip()
            if ts and value:
                points.append((ts, value))

    # Chunk boundaries can repeat a timestamp; keep one row per instant.
    deduped = dict(points)
    return sorted(deduped.items())

def fetch_meta(spans, agg):
    """The API's own description of what the numbers mean.

    Radar normalizes per response, so the scale is a property of the request,
    not of the world. Recording it next to the data keeps anything reading the
    CSV from inventing a unit.
    """
    payload = json.loads(request(spans, "json", agg))
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

HEADER = ["timestamp", "value"]

if args.start:
    if args.start > args.end:
        sys.exit(f"--start ({args.start}) is after --end ({args.end})")
    window_start, window_end = args.start, args.end
else:
    archive_start, window_end = previous_month(date.today())
    window_start = shift_months(archive_start, -(args.rolling_months - 1))

# The whole window arrives in ONE response, as parallel equal-length series.
# Three constraints force that shape:
#   * Radar normalizes per response, so values from separate requests sit on
#     different scales and can never be compared. Separate monthly requests --
#     what this script used to do -- gave every month its own maximum of 1.0.
#   * A single series covering the whole window is rejected at 1d resolution
#     ("aggregation interval is too low for date range").
#   * Series in one request must all be the same length ("comparisons must have
#     same duration as main series"), which rules out calendar months.
# Equal-length chunks in one request satisfy all three: each is short enough
# for daily data, and one response means one maximum for every day in it.
spans = equal_spans(window_start, window_end)
points = fetch_multi(spans, args.agg)
print()

# The earliest chunk can reach before the window; keep only what was asked for.
cutoff = window_start.isoformat()
rows = [[ts, value] for ts, value in points if ts[:10] >= cutoff]
if args.start:
    written = [write_csv(dated_name(window_start, window_end), HEADER, rows)]
else:
    prefix = archive_start.isoformat()[:7]
    archive_rows = [r for r in rows if r[0].startswith(prefix)]
    written = [
        write_csv(dated_name(archive_start, window_end), HEADER, archive_rows),
        write_csv(
            f"cloudflare_radar_ai_bots_worldwide_daily_last_{args.rolling_months}_months.csv",
            HEADER,
            rows,
        ),
    ]

# radar_meta.json describes the rolling window that the published page charts.
# A one-off backfill is a different window on a different scale, so it must not
# overwrite it -- doing so would have the page cite metadata for data it is not
# showing.
if args.start:
    print("Backfill run: leaving radar_meta.json alone (it describes the rolling window).")
else:
    meta_path = os.path.join(args.out_dir, "radar_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": "All months were fetched as parallel series in a single "
                        "request, so they share one MIN0_MAX maximum and are "
                        "comparable to each other. Values from a DIFFERENT run or "
                        "file are on a different scale and are not comparable to "
                        "these.",
                "series": {
                    "window": {
                        "start": window_start.isoformat(),
                        "end": window_end.isoformat(),
                        "aggInterval": args.agg,
                        "series_count": len(spans),
                        "comparable_across_months": True,
                        **fetch_meta(spans, args.agg),
                    }
                },
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
