#!/usr/bin/env python3

from __future__ import annotations

import calendar as calendar_module
import csv
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ANCHOR_YEAR = 2000
MAX_WORKERS = 16
FETCH_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 1.5
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
OPEN_METEO_END_DATE_BACKOFF_DAYS = 14
OPEN_METEO_WINDOW_DAYS = 365
OPEN_METEO_PRIOR_YEAR_COUNT = 10
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "site.json"


@dataclass
class MonthlySeries:
  year: int
  month: int
  status: str
  values: list[float | None]


class NoDataLoadedError(RuntimeError):
  """Raised when NOAA returns no usable month data for a series."""


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


def utc_now() -> datetime:
  return datetime.now(timezone.utc)


def fetch_url(url: str) -> str:
  request = Request(url, headers={"User-Agent": "heat-watch-updater/1.0"})
  last_error: HTTPError | URLError | None = None

  for attempt in range(FETCH_RETRIES):
    try:
      with urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")
    except HTTPError as error:
      if error.code not in RETRYABLE_HTTP_STATUS_CODES:
        raise
      last_error = error
    except URLError as error:
      last_error = error

    if attempt < FETCH_RETRIES - 1:
      time.sleep(BASE_RETRY_DELAY_SECONDS * (2**attempt))

  if last_error is not None:
    raise last_error

  raise RuntimeError(f"Failed to fetch NOAA URL: {url}")


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
  current_year = utc_now().year
  current_month = utc_now().month
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
  current_year = utc_now().year
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
    raise NoDataLoadedError("No NOAA data could be loaded.")

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
      "generatedAt": utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
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


def round_optional(value: Any, digits: int = 1) -> float | None:
  if value is None:
    return None

  number = float(value)
  if math.isnan(number):
    return None

  return round(number, digits)


def fetch_open_meteo_location(
  source: dict[str, Any],
  location: dict[str, Any],
  start_date: date,
  end_date: date,
) -> dict[str, Any]:
  params = {
    "latitude": location["latitude"],
    "longitude": location["longitude"],
    "start_date": start_date.isoformat(),
    "end_date": end_date.isoformat(),
    "daily": ",".join(source["dailyVariables"]),
    "timezone": location.get("timezone", "auto"),
    "temperature_unit": source.get("temperatureUnit", "celsius"),
    "precipitation_unit": source.get("precipitationUnit", "mm"),
  }
  url = f"{source['baseUrl']}?{urlencode(params)}"
  return json.loads(fetch_url(url))


def extract_open_meteo_location_values(
  location: dict[str, Any],
  response: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
  daily = response.get("daily") or {}
  dates = daily.get("time") or []

  def daily_values(variable: str, digits: int = 1) -> list[float | None]:
    raw_values = daily.get(variable) or []
    return [
      round_optional(raw_values[index], digits) if index < len(raw_values) else None
      for index in range(len(dates))
    ]

  return dates, {
    "id": location["id"],
    "name": location["name"],
    "latitude": location["latitude"],
    "longitude": location["longitude"],
    "timezone": location.get("timezone", response.get("timezone")),
    "grid": {
      "latitude": round_optional(response.get("latitude"), 4),
      "longitude": round_optional(response.get("longitude"), 4),
      "elevationMeters": round_optional(response.get("elevation"), 1),
    },
    "values": {
      "temperatureMaxC": daily_values("temperature_2m_max"),
      "temperatureMinC": daily_values("temperature_2m_min"),
      "relativeHumidityMeanPct": daily_values("relative_humidity_2m_mean"),
      "precipitationSumMm": daily_values("precipitation_sum", 2),
    },
  }


def build_open_meteo_windows(now_date: date) -> list[dict[str, Any]]:
  current_end_date = now_date - timedelta(days=1)
  current_start_date = current_end_date - timedelta(days=OPEN_METEO_WINDOW_DAYS - 1)
  windows = [
    {
      "id": "current",
      "label": "Current",
      "kind": "trailing-365",
      "startDate": current_start_date,
      "endDate": current_end_date,
      "days": OPEN_METEO_WINDOW_DAYS,
      "requestedStartDate": current_start_date,
      "requestedEndDate": current_end_date,
    },
  ]

  for year in range(now_date.year - 1, now_date.year - OPEN_METEO_PRIOR_YEAR_COUNT - 1, -1):
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)
    windows.append({
      "id": str(year),
      "label": str(year),
      "kind": "calendar-year",
      "startDate": start_date,
      "endDate": end_date,
      "days": (end_date - start_date).days + 1,
    })

  return windows


def serialize_open_meteo_window(window: dict[str, Any]) -> dict[str, Any]:
  serialized = {
    "id": window["id"],
    "label": window["label"],
    "kind": window["kind"],
    "startDate": window["startDate"].isoformat(),
    "endDate": window["endDate"].isoformat(),
    "days": window["days"],
  }

  requested_start_date = window.get("requestedStartDate")
  requested_end_date = window.get("requestedEndDate")
  if requested_start_date is not None and requested_end_date is not None:
    serialized["requestedStartDate"] = requested_start_date.isoformat()
    serialized["requestedEndDate"] = requested_end_date.isoformat()

  return serialized


def validate_open_meteo_window_values(
  series_slug: str,
  window: dict[str, Any],
  location_values: dict[str, Any],
  dates: list[str],
) -> None:
  if len(dates) != window["days"]:
    raise NoDataLoadedError(
      f"Open-Meteo returned {len(dates)} days for {series_slug} "
      f"{window['label']}; expected {window['days']}.",
    )

  values = location_values["values"]
  for key, value_series in values.items():
    if len(value_series) != len(dates):
      raise NoDataLoadedError(
        f"Open-Meteo returned {len(value_series)} {key} values for "
        f"{location_values['name']} {window['label']}; expected {len(dates)}.",
      )

  if not any(value is not None for value_series in values.values() for value in value_series):
    raise NoDataLoadedError(f"No Open-Meteo data loaded for {location_values['name']} {window['label']}.")


def fetch_open_meteo_window(
  series_slug: str,
  source: dict[str, Any],
  locations_config: list[dict[str, Any]],
  window: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
  candidate_offsets = range(OPEN_METEO_END_DATE_BACKOFF_DAYS + 1) if window["id"] == "current" else range(1)
  last_error: HTTPError | None = None

  for day_offset in candidate_offsets:
    candidate_window = dict(window)
    if day_offset:
      end_date = window["endDate"] - timedelta(days=day_offset)
      candidate_window["endDate"] = end_date
      candidate_window["startDate"] = end_date - timedelta(days=window["days"] - 1)

    try:
      aligned_dates: list[str] | None = None
      locations: list[dict[str, Any]] = []

      for location in locations_config:
        response = fetch_open_meteo_location(
          source,
          location,
          candidate_window["startDate"],
          candidate_window["endDate"],
        )
        dates, location_values = extract_open_meteo_location_values(location, response)

        validate_open_meteo_window_values(series_slug, candidate_window, location_values, dates)

        if aligned_dates is None:
          aligned_dates = dates
        elif dates != aligned_dates:
          raise RuntimeError(
            f"Open-Meteo returned misaligned dates for {location['name']} {candidate_window['label']}.",
          )

        locations.append(location_values)

      if not aligned_dates:
        raise NoDataLoadedError(f"No Open-Meteo dates loaded for {series_slug} {candidate_window['label']}.")

      candidate_window["startDate"] = date.fromisoformat(aligned_dates[0])
      candidate_window["endDate"] = date.fromisoformat(aligned_dates[-1])
      return candidate_window, aligned_dates, locations
    except HTTPError as error:
      if window["id"] != "current" or error.code != 400:
        raise
      last_error = error

  if last_error is not None:
    raise last_error

  raise NoDataLoadedError(f"No Open-Meteo data loaded for {series_slug} {window['label']}.")


def build_open_meteo_weather_story_payload(series: dict[str, Any]) -> dict[str, Any]:
  source = series["source"]
  windows = build_open_meteo_windows(utc_now().date())
  locations_config = source.get("locations") or []

  if not locations_config:
    raise ValueError(f"No locations configured for {series['slug']}.")

  locations_by_id: dict[str, dict[str, Any]] = {}
  resolved_windows: list[dict[str, Any]] = []
  default_window: dict[str, Any] | None = None

  for window in windows:
    resolved_window, dates, window_locations = fetch_open_meteo_window(
      series["slug"],
      source,
      locations_config,
      window,
    )
    resolved_windows.append(resolved_window)
    if resolved_window["id"] == "current":
      default_window = resolved_window

    for location_values in window_locations:
      location_entry = locations_by_id.setdefault(
        location_values["id"],
        {
          "id": location_values["id"],
          "name": location_values["name"],
          "latitude": location_values["latitude"],
          "longitude": location_values["longitude"],
          "timezone": location_values["timezone"],
          "grid": location_values["grid"],
          "windows": {},
        },
      )
      location_entry["windows"][resolved_window["id"]] = {
        "dates": dates,
        "values": location_values["values"],
      }

  if default_window is None:
    raise NoDataLoadedError(f"No current Open-Meteo data could be loaded for {series['slug']}.")

  locations = [locations_by_id[location["id"]] for location in locations_config]

  return {
    "metadata": {
      "slug": series["slug"],
      "title": series["title"],
      "subtitle": series["subtitle"],
      "source": source["provider"],
      "sourceUrl": source["baseUrl"],
      "dailyVariables": source["dailyVariables"],
      "units": {
        "temperature": "degC",
        "relativeHumidity": "percent",
        "precipitation": "mm",
      },
      "defaultWindowId": "current",
      "windowDays": default_window["days"],
      "startDate": default_window["startDate"].isoformat(),
      "endDate": default_window["endDate"].isoformat(),
      "requestedStartDate": default_window["requestedStartDate"].isoformat(),
      "requestedEndDate": default_window["requestedEndDate"].isoformat(),
      "windows": [serialize_open_meteo_window(window) for window in resolved_windows],
      "generatedAt": utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    },
    "locations": locations,
  }


def build_payload(series: dict[str, Any]) -> dict[str, Any]:
  if series["kind"] == "multi-location-weather-story":
    source_type = series["source"]["type"]
    if source_type != "open-meteo-historical-window":
      raise NotImplementedError(f"Unsupported source type: {source_type}")

    return build_open_meteo_weather_story_payload(series)

  if series["kind"] != "seasonal-temperature-chart":
    raise NotImplementedError(f'Unsupported series kind: {series["kind"]}')

  source_type = series["source"]["type"]
  if source_type != "nclimgrid-region-tmax":
    raise NotImplementedError(f"Unsupported source type: {source_type}")

  return build_nclimgrid_region_payload(series)


def main() -> int:
  catalog = load_catalog()
  requested_slugs = set(sys.argv[1:])
  processed_slugs: set[str] = set()

  for series in catalog["series"]:
    if requested_slugs and series["slug"] not in requested_slugs:
      continue

    processed_slugs.add(series["slug"])
    output_path = ROOT / series["dataPath"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
      payload = build_payload(series)
    except NoDataLoadedError:
      if not output_path.exists():
        raise
      try:
        existing_payload = json.loads(output_path.read_text(encoding="utf-8"))
      except json.JSONDecodeError as error:
        raise RuntimeError(
          f"No NOAA data loaded for {series['slug']} and the existing dataset is invalid.",
        ) from error

      metadata = existing_payload.get("metadata", {})
      latest_data_date = metadata.get("latestDataDate") or metadata.get("endDate", "unknown")
      print(
        f"No data loaded for {series['slug']}; keeping existing dataset at {output_path} "
        f"(latestDate={latest_data_date}).",
        file=sys.stderr,
      )
      continue

    output_path.write_text(
      json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
      encoding="utf-8",
    )
    print(f"Wrote {output_path}")

  missing_slugs = requested_slugs - processed_slugs
  if missing_slugs:
    raise RuntimeError(f"Unknown series slug(s): {', '.join(sorted(missing_slugs))}")

  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except Exception as error:  # noqa: BLE001
    print(f"Failed to update dataset: {error}", file=sys.stderr)
    raise
