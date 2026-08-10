#!/usr/bin/env python3
"""
Nord Pool day-ahead price scraper (Latvia by default).

Pulls the JSON that data.nordpoolgroup.com's price page loads behind the scenes
and appends it to a CSV, keyed on (delivery_start_utc, area) so re-runs never
duplicate rows.

Usage
-----
  # one-off backfill (inclusive of both ends)
  python nordpool_lv.py backfill --from 2026-05-09 --to today

  # daily run: fetches yesterday .. tomorrow, fills any gaps, dedupes
  python nordpool_lv.py daily

  # single date, printed to stdout instead of written
  python nordpool_lv.py backfill --from 2026-06-10 --to 2026-06-10 --dry-run

  # cross-check against Elering's open Baltic API
  python nordpool_lv.py backfill --from 2026-05-09 --to today --source elering

Options worth knowing
---------------------
  --areas LV,EE,LT      several bidding zones in one go
  --currency EUR        EUR / NOK / SEK / DKK / PLN
  --out prices.csv      output path
  --sleep 1.0           delay between requests, be polite
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

NORDPOOL_URL = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
ELERING_URL = "https://dashboard.elering.ee/api/nps/price"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://data.nordpoolgroup.com",
    "Referer": "https://data.nordpoolgroup.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

FIELDS = [
    "delivery_start_utc",
    "delivery_end_utc",
    "delivery_date",
    "area",
    "price",
    "currency",
    "resolution_min",
    "source",
    "fetched_at_utc",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_date(s: str) -> dt.date:
    if s.lower() in ("today", "now"):
        return dt.date.today()
    if s.lower() == "yesterday":
        return dt.date.today() - dt.timedelta(days=1)
    if s.lower() == "tomorrow":
        return dt.date.today() + dt.timedelta(days=1)
    return dt.date.fromisoformat(s)


def daterange(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def iso_z(s: str) -> str:
    """Normalise the API's timestamp strings to '...+00:00' form."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).isoformat()


def get_json(url: str, params: dict, retries: int = 4, timeout: int = 30):
    """GET with exponential backoff. Returns None on a clean 204/404 (no data yet)."""
    delay = 2.0
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code in (204, 404):
                return None
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", delay))
                print(f"    rate limited, waiting {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                delay *= 2
                continue
            r.raise_for_status()
            if not r.text.strip():
                return None
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == retries - 1:
                break
            sleep_for = delay + random.uniform(0, 1)
            print(f"    {type(e).__name__}: {e} — retrying in {sleep_for:.1f}s",
                  file=sys.stderr)
            time.sleep(sleep_for)
            delay *= 2
    raise RuntimeError(f"failed after {retries} attempts: {last}")


# --------------------------------------------------------------------------
# source: nord pool
# --------------------------------------------------------------------------

def fetch_nordpool(date: dt.date, areas: list[str], currency: str) -> list[dict]:
    """
    One delivery date. Resolution-agnostic: whatever market time units the API
    returns (60-min historically, 15-min since the EU MTU switch) come back as
    individual rows with their own start/end.
    """
    payload = get_json(NORDPOOL_URL, {
        "market": "DayAhead",
        "deliveryArea": ",".join(areas),
        "currency": currency,
        "date": date.isoformat(),
    })
    if not payload:
        return []

    entries = payload.get("multiAreaEntries") or []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []

    for e in entries:
        try:
            start = iso_z(e["deliveryStart"])
            end = iso_z(e["deliveryEnd"])
        except (KeyError, ValueError):
            continue
        mins = int(
            (dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start))
            .total_seconds() // 60
        )
        for area, price in (e.get("entryPerArea") or {}).items():
            if price is None:
                continue
            rows.append({
                "delivery_start_utc": start,
                "delivery_end_utc": end,
                "delivery_date": payload.get("deliveryDateCET", date.isoformat()),
                "area": area,
                "price": price,
                "currency": payload.get("currency", currency),
                "resolution_min": mins,
                "source": "nordpool",
                "fetched_at_utc": now,
            })
    return rows


# --------------------------------------------------------------------------
# source: elering (open Baltic API, no key, good cross-check / fallback)
# --------------------------------------------------------------------------

ELERING_CHUNK_DAYS = 180   # a full year works, but 400s appear on multi-year spans


def _elering_chunk(start: dt.date, end: dt.date, areas: list[str]) -> list[dict]:
    """One request. `end` is exclusive here — the caller handles the +1 day."""
    payload = get_json(ELERING_URL, {
        "start": f"{start.isoformat()}T00:00:00.000Z",
        "end": f"{end.isoformat()}T00:00:00.000Z",
    })
    if not payload or not payload.get("data"):
        return []

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []

    for area in areas:
        points = payload["data"].get(area.lower(), [])
        if not points:
            continue

        # Resolution is not stated in the response and changed with the EU's
        # move to 15-minute market time units, so infer it from the gap to the
        # next point. The final point inherits the previous gap.
        stamps = [p["timestamp"] for p in points]
        gaps = [
            stamps[i + 1] - stamps[i] if i + 1 < len(stamps) else None
            for i in range(len(stamps))
        ]
        for i in range(len(gaps) - 1, -1, -1):
            if gaps[i] is None or gaps[i] <= 0:
                gaps[i] = gaps[i + 1] if i + 1 < len(gaps) else 3600

        for point, gap in zip(points, gaps):
            start_dt = dt.datetime.fromtimestamp(point["timestamp"], dt.timezone.utc)
            end_dt = start_dt + dt.timedelta(seconds=gap)
            rows.append({
                "delivery_start_utc": start_dt.isoformat(),
                "delivery_end_utc": end_dt.isoformat(),
                "delivery_date": start_dt.date().isoformat(),
                "area": area.upper(),
                "price": point["price"],
                "currency": "EUR",
                "resolution_min": gap // 60,
                "source": "elering",
                "fetched_at_utc": now,
            })
    return rows


def fetch_elering(start: dt.date, end: dt.date, areas: list[str]) -> list[dict]:
    """
    Elering rejects very long spans with a 400, so split the range into chunks.
    Overlapping seams are harmless — append_rows dedupes on the timestamp.
    """
    stop = end + dt.timedelta(days=1)       # make the range inclusive of `end`
    rows: list[dict] = []
    cursor = start

    while cursor < stop:
        chunk_end = min(cursor + dt.timedelta(days=ELERING_CHUNK_DAYS), stop)
        got = _elering_chunk(cursor, chunk_end, areas)
        print(f"  {cursor} .. {chunk_end - dt.timedelta(days=1)}  {len(got):>6} rows")
        rows.extend(got)
        cursor = chunk_end
        if cursor < stop:
            time.sleep(1.0)

    return rows


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def load_keys(path: Path) -> set:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {
            (r["delivery_start_utc"], r["area"], r["source"])
            for r in csv.DictReader(f)
        }


def append_rows(path: Path, rows: list[dict], known: set) -> int:
    fresh = []
    for r in rows:
        k = (r["delivery_start_utc"], r["area"], r["source"])
        if k in known:
            continue
        known.add(k)
        fresh.append(r)
    if not fresh:
        return 0

    new_file = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(fresh)
    return len(fresh)


def sort_csv(path: Path) -> None:
    """Keep the file chronological after out-of-order appends."""
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: (r["delivery_start_utc"], r["area"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args) -> int:
    areas = [a.strip().upper() for a in args.areas.split(",") if a.strip()]
    out = Path(args.out)
    known = load_keys(out)

    if args.command == "daily":
        start = dt.date.today() - dt.timedelta(days=args.lookback)
        end = dt.date.today() + dt.timedelta(days=1)  # tomorrow, published ~12:45 CET
    else:
        start, end = parse_date(args.date_from), parse_date(args.date_to)

    if start > end:
        print("error: --from is after --to", file=sys.stderr)
        return 2

    print(f"{args.source} | {','.join(areas)} | {start} .. {end} -> {out}")

    total_new = 0
    missing: list[str] = []

    if args.source == "elering":
        rows = fetch_elering(start, end, areas)
        if args.dry_run:
            print(json.dumps(rows[:8], indent=2))
            print(f"({len(rows)} rows, not written)")
            return 0
        total_new = append_rows(out, rows, known)
    else:
        for d in daterange(start, end):
            try:
                rows = fetch_nordpool(d, areas, args.currency)
            except RuntimeError as e:
                print(f"  {d}  ERROR  {e}", file=sys.stderr)
                missing.append(d.isoformat())
                continue

            if not rows:
                # normal for tomorrow before the auction result is published
                print(f"  {d}  no data yet")
                missing.append(d.isoformat())
            elif args.dry_run:
                print(json.dumps(rows[:8], indent=2))
                print(f"({len(rows)} rows, not written)")
            else:
                n = append_rows(out, rows, known)
                total_new += n
                print(f"  {d}  {len(rows):>4} rows  ({n} new)")

            time.sleep(args.sleep)

    if not args.dry_run:
        sort_csv(out)
        print(f"\ndone: {total_new} new rows in {out}")

    if missing:
        print(f"no data for: {', '.join(missing)}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--areas", default="LV")
        sp.add_argument("--currency", default="EUR")
        sp.add_argument("--out", default="nordpool_prices.csv")
        sp.add_argument("--source", choices=["nordpool", "elering"], default="nordpool")
        sp.add_argument("--sleep", type=float, default=1.0)
        sp.add_argument("--dry-run", action="store_true")

    b = sub.add_parser("backfill")
    b.add_argument("--from", dest="date_from", required=True)
    b.add_argument("--to", dest="date_to", default="today")
    common(b)

    d = sub.add_parser("daily")
    d.add_argument("--lookback", type=int, default=3,
                   help="also re-check the last N days to catch late corrections")
    common(d)

    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
