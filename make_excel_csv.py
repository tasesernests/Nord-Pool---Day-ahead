#!/usr/bin/env python3
"""
Turn the raw scraper output into a flat, Excel-friendly CSV.

Reads nordpool_prices.csv (UTC timestamps, several bookkeeping columns) and
writes a clean three-column file in Latvian local time:

    Day,Time,Price
    2026-06-10,01:00,99.95
    2026-06-10,01:15,110.18

Usage
-----
    python make_excel_csv.py nordpool_prices.csv prices_riga.csv

    # keep EUR/MWh instead of converting, and add an hourly-average file
    python make_excel_csv.py in.csv out.csv --unit mwh --hourly hourly.csv

Note for Windows: the timezone database may not be installed. If you get a
ZoneInfoNotFoundError, run  pip install tzdata
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

RIGA = ZoneInfo("Europe/Riga")


def convert(in_path: Path, area: str, unit: str) -> list[dict]:
    if not in_path.exists():
        sys.exit(f"error: {in_path} not found — run the scraper first")

    with in_path.open(newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))

    # If both sources ended up in the file, prefer Nord Pool for any timestamp
    # that appears twice.
    best: dict[str, dict] = {}
    for r in raw:
        if r["area"].upper() != area.upper():
            continue
        key = r["delivery_start_utc"]
        if key in best and best[key]["source"] == "nordpool":
            continue
        best[key] = r

    rows = []
    for r in best.values():
        local = dt.datetime.fromisoformat(r["delivery_start_utc"]).astimezone(RIGA)
        price = float(r["price"])
        if unit == "kwh":
            price = round(price / 10, 4)   # EUR/MWh -> cents per kWh
        rows.append({
            "Day": local.date().isoformat(),
            "Time": local.strftime("%H:%M"),
            "Price": price,
        })

    rows.sort(key=lambda r: (r["Day"], r["Time"]))
    return rows


def hourly_average(rows: list[dict]) -> list[dict]:
    """Collapse 15-minute periods into hourly averages."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        buckets[(r["Day"], r["Time"][:2] + ":00")].append(r["Price"])
    out = [
        {"Day": d, "Time": h, "Price": round(sum(v) / len(v), 4)}
        for (d, h), v in buckets.items()
    ]
    out.sort(key=lambda r: (r["Day"], r["Time"]))
    return out


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Day", "Time", "Price"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("infile", nargs="?", default="nordpool_prices.csv")
    p.add_argument("outfile", nargs="?", default="prices_riga.csv")
    p.add_argument("--area", default="LV")
    p.add_argument("--unit", choices=["mwh", "kwh"], default="mwh",
                   help="mwh = EUR/MWh as published; kwh = cents per kWh")
    p.add_argument("--hourly", metavar="PATH",
                   help="also write hourly averages to this path")
    a = p.parse_args()

    rows = convert(Path(a.infile), a.area, a.unit)
    if not rows:
        sys.exit(f"error: no rows for area {a.area} in {a.infile}")

    write(Path(a.outfile), rows)
    if a.hourly:
        write(Path(a.hourly), hourly_average(rows))


if __name__ == "__main__":
    main()
