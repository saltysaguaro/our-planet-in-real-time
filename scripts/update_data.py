#!/usr/bin/env python3

from __future__ import annotations

import calendar as calendar_module
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


FETCH_RETRIES = 8
BASE_RETRY_DELAY_SECONDS = 1.5
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
RATE_LIMIT_RETRY_DELAY_SECONDS = 30
OPEN_METEO_END_DATE_BACKOFF_DAYS = 14
OPEN_METEO_WINDOW_DAYS = 365
OPEN_METEO_PRIOR_YEAR_COUNT = 10
OPEN_METEO_ANNUAL_HISTORY_START_YEAR = 1940
OPEN_METEO_ANNUAL_HISTORY_CHUNK_YEARS = 100
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "site.json"


class NoDataLoadedError(RuntimeError):
  """Raised when a data source returns no usable data for a series."""


def utc_now() -> datetime:
  return datetime.now(timezone.utc)


def fetch_url(url: str) -> str:
  request = Request(url, headers={"User-Agent": "our-planet-data-updater/1.0"})
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
      retry_after = None
      if isinstance(last_error, HTTPError) and last_error.code == 429:
        retry_after_header = last_error.headers.get("Retry-After")
        if retry_after_header:
          try:
            retry_after = float(retry_after_header)
          except ValueError:
            retry_after = None

      if isinstance(last_error, HTTPError) and last_error.code == 429:
        time.sleep(retry_after or RATE_LIMIT_RETRY_DELAY_SECONDS)
      else:
        time.sleep(BASE_RETRY_DELAY_SECONDS * (2**attempt))

  if last_error is not None:
    raise last_error

  raise RuntimeError(f"Failed to fetch data URL: {url}")


def load_catalog() -> dict[str, Any]:
  return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


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
  daily_variables: list[str] | None = None,
) -> dict[str, Any]:
  params = {
    "latitude": location["latitude"],
    "longitude": location["longitude"],
    "start_date": start_date.isoformat(),
    "end_date": end_date.isoformat(),
    "daily": ",".join(daily_variables or source["dailyVariables"]),
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


def iter_year_chunks(start_year: int, end_year: int) -> Iterable[tuple[int, int]]:
  chunk_start = start_year
  while chunk_start <= end_year:
    chunk_end = min(end_year, chunk_start + OPEN_METEO_ANNUAL_HISTORY_CHUNK_YEARS - 1)
    yield chunk_start, chunk_end
    chunk_start = chunk_end + 1


def extract_open_meteo_annual_precipitation(
  response: dict[str, Any],
  start_year: int,
  end_year: int,
) -> list[dict[str, Any]]:
  daily = response.get("daily") or {}
  dates = daily.get("time") or []
  precipitation = daily.get("precipitation_sum") or []
  totals = {year: 0.0 for year in range(start_year, end_year + 1)}
  date_counts = {year: 0 for year in range(start_year, end_year + 1)}
  valid_counts = {year: 0 for year in range(start_year, end_year + 1)}

  for index, date_text in enumerate(dates):
    year = int(date_text[:4])
    if year < start_year or year > end_year:
      continue

    date_counts[year] += 1
    if index >= len(precipitation) or precipitation[index] is None:
      continue

    value = float(precipitation[index])
    if math.isnan(value):
      continue

    totals[year] += value
    valid_counts[year] += 1

  annual_values = []
  for year in range(start_year, end_year + 1):
    expected_day_count = 366 if calendar_module.isleap(year) else 365
    if date_counts[year] != expected_day_count or valid_counts[year] != expected_day_count:
      continue

    annual_values.append({
      "year": year,
      "precipitationSumMm": round(totals[year], 1),
      "validDayCount": valid_counts[year],
      "expectedDayCount": expected_day_count,
    })

  return annual_values


def fetch_open_meteo_annual_precipitation_chunk(
  source: dict[str, Any],
  location: dict[str, Any],
  start_year: int,
  end_year: int,
) -> list[dict[str, Any]]:
  response = fetch_open_meteo_location(
    source,
    location,
    date(start_year, 1, 1),
    date(end_year, 12, 31),
    daily_variables=["precipitation_sum"],
  )
  return extract_open_meteo_annual_precipitation(response, start_year, end_year)


def fetch_open_meteo_annual_precipitation_with_fallback(
  source: dict[str, Any],
  location: dict[str, Any],
  start_year: int,
  end_year: int,
) -> list[dict[str, Any]]:
  try:
    return fetch_open_meteo_annual_precipitation_chunk(source, location, start_year, end_year)
  except HTTPError as error:
    if error.code != 400:
      raise
    if start_year == end_year:
      return []

  midpoint_year = (start_year + end_year) // 2
  return [
    *fetch_open_meteo_annual_precipitation_with_fallback(source, location, start_year, midpoint_year),
    *fetch_open_meteo_annual_precipitation_with_fallback(source, location, midpoint_year + 1, end_year),
  ]


def annual_precipitation_history_records(
  history: dict[str, Any] | None,
  end_year: int,
) -> list[dict[str, Any]]:
  if not history:
    return []

  years = history.get("years") or []
  totals = history.get("precipitationSumMm") or []
  valid_day_counts = history.get("validDayCounts") or []
  expected_day_counts = history.get("expectedDayCounts") or []
  records_by_year: dict[int, dict[str, Any]] = {}

  for index, raw_year in enumerate(years):
    if index >= len(totals) or totals[index] is None:
      continue

    year = int(raw_year)
    if year > end_year:
      continue

    expected_day_count = (
      int(expected_day_counts[index])
      if index < len(expected_day_counts)
      else 366 if calendar_module.isleap(year) else 365
    )
    valid_day_count = (
      int(valid_day_counts[index])
      if index < len(valid_day_counts)
      else expected_day_count
    )

    if valid_day_count != expected_day_count:
      continue

    records_by_year[year] = {
      "year": year,
      "precipitationSumMm": round(float(totals[index]), 1),
      "validDayCount": valid_day_count,
      "expectedDayCount": expected_day_count,
    }

  return [records_by_year[year] for year in sorted(records_by_year)]


def serialize_annual_precipitation_history(annual_values: list[dict[str, Any]]) -> dict[str, Any]:
  return {
    "startYear": annual_values[0]["year"],
    "endYear": annual_values[-1]["year"],
    "years": [item["year"] for item in annual_values],
    "precipitationSumMm": [item["precipitationSumMm"] for item in annual_values],
    "validDayCounts": [item["validDayCount"] for item in annual_values],
    "expectedDayCounts": [item["expectedDayCount"] for item in annual_values],
  }


def build_open_meteo_annual_precipitation_history(
  source: dict[str, Any],
  location: dict[str, Any],
  end_year: int,
  existing_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
  start_year = int(source.get("annualHistoryStartYear", OPEN_METEO_ANNUAL_HISTORY_START_YEAR))
  annual_values = annual_precipitation_history_records(existing_history, end_year)
  fetch_start_year = max((item["year"] for item in annual_values), default=start_year - 1) + 1

  if fetch_start_year <= end_year:
    for chunk_start, chunk_end in iter_year_chunks(fetch_start_year, end_year):
      annual_values.extend(fetch_open_meteo_annual_precipitation_with_fallback(
        source,
        location,
        chunk_start,
        chunk_end,
      ))
      time.sleep(0.2)

    annual_values.sort(key=lambda item: item["year"])

  if not annual_values:
    raise NoDataLoadedError(f"No annual precipitation history loaded for {location['name']}.")

  return serialize_annual_precipitation_history(annual_values)


def existing_location_annual_precipitation(
  existing_payload: dict[str, Any] | None,
  location_id: str,
) -> dict[str, Any] | None:
  if not existing_payload:
    return None

  for location in existing_payload.get("locations", []):
    if location.get("id") == location_id:
      return location.get("annualPrecipitation")

  return None


def build_open_meteo_weather_story_payload(
  series: dict[str, Any],
  existing_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
  source = series["source"]
  windows = build_open_meteo_windows(utc_now().date())
  locations_config = source.get("locations") or []
  annual_precipitation_end_year = utc_now().year - 1

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

  for location in locations_config:
    locations_by_id[location["id"]]["annualPrecipitation"] = build_open_meteo_annual_precipitation_history(
      source,
      location,
      annual_precipitation_end_year,
      existing_location_annual_precipitation(existing_payload, location["id"]),
    )

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
      "annualPrecipitationStartYear": source.get(
        "annualHistoryStartYear",
        OPEN_METEO_ANNUAL_HISTORY_START_YEAR,
      ),
      "annualPrecipitationEndYear": annual_precipitation_end_year,
      "generatedAt": utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    },
    "locations": locations,
  }


def build_payload(series: dict[str, Any], existing_payload: dict[str, Any] | None = None) -> dict[str, Any]:
  if series["kind"] != "multi-location-weather-story":
    raise NotImplementedError(f'Unsupported series kind: {series["kind"]}')

  source_type = series["source"]["type"]
  if source_type != "open-meteo-historical-window":
    raise NotImplementedError(f"Unsupported source type: {source_type}")

  return build_open_meteo_weather_story_payload(series, existing_payload)


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
    existing_payload: dict[str, Any] | None = None
    if output_path.exists():
      try:
        existing_payload = json.loads(output_path.read_text(encoding="utf-8"))
      except json.JSONDecodeError:
        existing_payload = None

    try:
      payload = build_payload(series, existing_payload)
    except NoDataLoadedError:
      if existing_payload is None:
        raise

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
