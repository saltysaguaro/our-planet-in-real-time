#!/usr/bin/env python3

from __future__ import annotations

import calendar as calendar_module
import csv
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ANCHOR_YEAR = 2000
MAX_WORKERS = 16
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "site.json"


@dataclass
class MonthlySeries:
  year: int
  month: int
  status: str
  values: list[float | None]


def build_anchor_calendar() -> list[str]:
  start = date(ANCHOR_YEAR, 1, 1)
  end = date(ANCHOR_YEAR, 12, 31)
  days = (end - start).days + 1
  return [(start.fromordinal(start.toordinal() + offset)).isoformat() for offset in range(days)]


CALENDAR = build_anchor_calendar()
CALENDAR_INDEX = {
  f"{entry[5:7]}-{entry[8:10]}": index
  for index, entry in enumerate(CALENDAR)
}


def fetch_url(url: str) -> str:
  request = Request(url, headers={"User-Agent": "heat-watch-updater/1.0"})
  with urlopen(request, timeout=60) as response:
    return response.read().decode("utf-8")


def load_catalog() -> dict[str, Any]:
  return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def fetch_month(base_url: str, region_id: str, variable: str, year: int, month: int) -> MonthlySeries | None:
  for status in ("scaled", "prelim"):
    url = f"{base_url}/{year}/tmax-{year}{month:02d}-reg-{status}.csv"

    try:
      text = fetch_url(url)
    except HTTPError as error:
      if error.code == 404:
        continue
      raise
    except URLError:
      raise

    rows = csv.reader(text.splitlines())
    for row in rows:
      if len(row) < 7:
        continue
      if row[0] != "reg" or row[1] != region_id or row[5] != variable:
        continue

      values = []
      for raw_value in row[6:37]:
        value = float(raw_value)
        if value <= -999:
          values.append(None)
        else:
          values.append(value)

      return MonthlySeries(year=year, month=month, status=status, values=values)

  return None


def iter_available_months(
  base_url: str,
  region_id: str,
  variable: str,
  start_year: int,
  end_year: int,
) -> Iterable[MonthlySeries]:
  current_year = datetime.utcnow().year
  current_month = datetime.utcnow().month
  month_requests: list[tuple[int, int]] = []

  for year in range(start_year, end_year + 1):
    final_month = current_month if year == current_year else 12
    for month in range(1, final_month + 1):
      month_requests.append((year, month))

  loaded: list[MonthlySeries] = []
  total = len(month_requests)

  with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {
      executor.submit(fetch_month, base_url, region_id, variable, year, month): (year, month)
      for year, month in month_requests
    }

    for index, future in enumerate(as_completed(futures), start=1):
      monthly = future.result()
      if monthly is not None:
        loaded.append(monthly)

      if index % 60 == 0 or index == total:
        print(f"Fetched {index}/{total} monthly files...", file=sys.stderr)

  for monthly in sorted(loaded, key=lambda item: (item.year, item.month)):
    yield monthly


def build_nclimgrid_region_payload(series: dict[str, Any]) -> dict[str, Any]:
  source = series["source"]
  base_url = source["baseUrl"]
  region_id = source["regionId"]
  region_name = source["regionName"]
  variable = source["variable"]
  start_year = int(source["startYear"])
  reference_start, reference_end = source["referencePeriod"]
  current_year = datetime.utcnow().year
  series_by_year: dict[int, list[float | None]] = {
    current_year: [None] * len(CALENDAR),
  }
  reference_values: dict[int, list[float]] = defaultdict(list)
  latest_date: date | None = None
  latest_value: float | None = None
  current_year_latest_date: date | None = None
  current_year_latest_value: float | None = None

  for monthly in iter_available_months(base_url, region_id, variable, start_year, current_year):
    year_values = series_by_year.setdefault(monthly.year, [None] * len(CALENDAR))
    days_in_month = calendar_module.monthrange(monthly.year, monthly.month)[1]

    for day in range(1, days_in_month + 1):
      if day > len(monthly.values):
        break

      value = monthly.values[day - 1]
      if value is None or math.isnan(value):
        continue

      key = f"{monthly.month:02d}-{day:02d}"
      calendar_index = CALENDAR_INDEX[key]
      year_values[calendar_index] = round(value, 2)

      actual_date = date(monthly.year, monthly.month, day)
      if reference_start <= monthly.year <= reference_end:
        reference_values[calendar_index].append(value)

      if latest_date is None or actual_date > latest_date:
        latest_date = actual_date
        latest_value = value

      if actual_date.year == current_year and (
        current_year_latest_date is None or actual_date > current_year_latest_date
      ):
        current_year_latest_date = actual_date
        current_year_latest_value = value

  if latest_date is None or latest_value is None:
    raise RuntimeError("No NOAA data could be loaded.")

  sorted_years = sorted(series_by_year)
  current_year_values = series_by_year[current_year]
  historical_end = current_year - 1
  historical = [
    {
      "year": year,
      "values": series_by_year[year],
    }
    for year in sorted(year for year in series_by_year if year < current_year)
  ]

  average_values = []
  for index in range(len(CALENDAR)):
    day_values = reference_values.get(index, [])
    if not day_values:
      average_values.append(None)
      continue
    average_values.append(round(sum(day_values) / len(day_values), 2))

  return {
    "metadata": {
      "slug": series["slug"],
      "title": series["title"],
      "subtitle": series["subtitle"],
      "source": source["provider"],
      "sourceUrl": base_url,
      "regionId": region_id,
      "regionName": region_name,
      "variable": variable,
      "units": source["units"],
      "historyStart": sorted_years[0],
      "historicalEnd": historical_end,
      "referencePeriod": [reference_start, reference_end],
      "latestDataDate": latest_date.isoformat(),
      "latestDataValue": round(latest_value, 2),
      "currentYearLatestDataDate": current_year_latest_date.isoformat() if current_year_latest_date else None,
      "currentYearLatestDataValue": round(current_year_latest_value, 2) if current_year_latest_value is not None else None,
      "generatedAt": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    },
    "calendar": CALENDAR,
    "historical": historical,
    "average": {
      "values": average_values,
    },
    "currentYear": {
      "year": current_year,
      "values": current_year_values,
    },
  }


def build_payload(series: dict[str, Any]) -> dict[str, Any]:
  if series["kind"] != "seasonal-temperature-chart":
    raise NotImplementedError(f'Unsupported series kind: {series["kind"]}')

  source_type = series["source"]["type"]
  if source_type != "nclimgrid-region-tmax":
    raise NotImplementedError(f"Unsupported source type: {source_type}")

  return build_nclimgrid_region_payload(series)


def main() -> int:
  catalog = load_catalog()

  for series in catalog["series"]:
    output_path = ROOT / series["dataPath"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(series)
    output_path.write_text(
      json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
      encoding="utf-8",
    )
    print(f"Wrote {output_path}")

  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as error:  # noqa: BLE001
    print(f"Failed to update dataset: {error}", file=sys.stderr)
    raise
