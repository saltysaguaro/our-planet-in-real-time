#!/usr/bin/env python3
"""Render a northern-Algeria-focused wildfire-smoke animation.

The scientific field is NASA GMAO GEOS-FP brown-carbon aerosol optical depth
(`brexttau`) from the public 0.25-degree seamless OPeNDAP collection.  The
visual treatment follows NASA SVS visualization 5666: the July Blue Marble:
Next Generation basemap, the released tan-to-deep-red legend, an hourly clock,
and a northern-Algeria-centered globe view.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw
from pydap.client import open_url

GEOS_URL = (
    "https://opendap.nccs.nasa.gov/dods/GEOS-5/fp/0.25_deg/"
    "seamless/tavg3_2d_aer_Nx.latest"
)
VARIABLE = "brexttau"
REQUEST_START = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
# "Through September 1" is interpreted as the complete UTC calendar day.
REQUEST_END = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
FPS = 30
LOW_WIDTH = 960
LOW_HEIGHT = 540
FINAL_WIDTH = 1920
FINAL_HEIGHT = 1080
AOD_LEGEND_MAX = 0.5

BUILD = Path("build")
SOURCE_FIRST = BUILD / "source-first.png"
BMNG_FILE = BUILD / "bmng-july-5400x2700.jpg"
INTERMEDIATE = BUILD / "northern-algeria-smoke-960x540.mp4"
FINAL_VIDEO = BUILD / "NASA_GEOS_FP_northern_Algeria_20260701_20260901_1920x1080.mp4"
DATA_FILE = BUILD / "GEOS_FP_brexttau_northern_Africa_20260701_20260901.npz"


def run(cmd: list[str], *, input_bytes: bytes | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, input=input_bytes, check=True)


def parse_grads_min(value: object) -> datetime:
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})z(\d{1,2})([a-z]{3})(\d{4})", text)
    if not match:
        raise ValueError(f"Unrecognized GrADS minimum time: {value!r}")
    hour, minute, day, month, year = match.groups()
    return datetime.strptime(
        f"{year}-{month}-{day} {hour}:{minute}", "%Y-%b-%d %H:%M"
    ).replace(tzinfo=timezone.utc)


def decode_time_axis(time_var: object) -> tuple[list[datetime], np.ndarray, dict[str, object]]:
    attrs = dict(getattr(time_var, "attributes", {}))
    raw = np.asarray(time_var[:].data, dtype=np.float64).reshape(-1)
    if raw.size < 2:
        raise RuntimeError("GEOS time coordinate has fewer than two values")

    if "grads_min" in attrs:
        start = parse_grads_min(attrs["grads_min"])
        step_hours = float(np.median(np.diff(raw)) * 24.0)
        times = [start + timedelta(hours=step_hours * i) for i in range(raw.size)]
    else:
        # GDS encodes time as day numbers whose value 2 corresponds to
        # 0001-01-01.  This fallback is retained for archive compatibility.
        epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
        times = [epoch + timedelta(days=float(value) - 2.0) for value in raw]
        step_hours = (times[1] - times[0]).total_seconds() / 3600.0

    if not (2.99 <= step_hours <= 3.01):
        raise RuntimeError(f"Expected a 3-hour GEOS collection; found {step_hours} h")
    return times, raw, attrs


def nearest_bracketing_indices(
    times: list[datetime], start: datetime, end: datetime
) -> tuple[int, int]:
    seconds = np.asarray([(item - times[0]).total_seconds() for item in times])
    start_seconds = (start - times[0]).total_seconds()
    end_seconds = (end - times[0]).total_seconds()
    if start_seconds < seconds[0] or end_seconds > seconds[-1]:
        raise RuntimeError(
            f"Requested range {start.isoformat()}..{end.isoformat()} is outside "
            f"available range {times[0].isoformat()}..{times[-1].isoformat()}"
        )
    i0 = max(0, int(np.searchsorted(seconds, start_seconds, side="right") - 1))
    i1 = min(len(times) - 1, int(np.searchsorted(seconds, end_seconds, side="left")))
    return i0, i1


def fetch_data_chunks(
    variable: object,
    time_start: int,
    time_stop_inclusive: int,
    lat_start: int,
    lat_stop_exclusive: int,
    lon_start: int,
    lon_stop_exclusive: int,
    *,
    chunk_size: int = 32,
) -> np.ndarray:
    chunks: list[np.ndarray] = []
    expected_y = lat_stop_exclusive - lat_start
    expected_x = lon_stop_exclusive - lon_start
    for chunk_start in range(time_start, time_stop_inclusive + 1, chunk_size):
        chunk_stop = min(time_stop_inclusive + 1, chunk_start + chunk_size)
        print(
            f"Downloading {VARIABLE} time indices {chunk_start}:{chunk_stop}...",
            flush=True,
        )
        last_error: Exception | None = None
        for attempt in range(1, 5):
            try:
                subset = variable[
                    chunk_start:chunk_stop,
                    lat_start:lat_stop_exclusive,
                    lon_start:lon_stop_exclusive,
                ]
                array = np.asarray(subset.data, dtype=np.float32)
                array = np.squeeze(array)
                if array.ndim == 2:
                    array = array[np.newaxis, :, :]
                expected_t = chunk_stop - chunk_start
                if array.shape != (expected_t, expected_y, expected_x):
                    raise RuntimeError(
                        f"Unexpected chunk shape {array.shape}; expected "
                        f"{(expected_t, expected_y, expected_x)}"
                    )
                chunks.append(array)
                last_error = None
                break
            except Exception as exc:  # pragma: no cover - network retry path
                last_error = exc
                print(f"Attempt {attempt} failed: {exc}", file=sys.stderr, flush=True)
                time.sleep(3 * attempt)
        if last_error is not None:
            raise RuntimeError(f"Could not retrieve GEOS data chunk: {last_error}")

    data = np.concatenate(chunks, axis=0)
    invalid = (~np.isfinite(data)) | (data < 0.0) | (data > 1.0e10)
    data[invalid] = 0.0
    return data


def inverse_orthographic_maps(
    width: int,
    height: int,
    *,
    center_lon_deg: float = 10.0,
    center_lat_deg: float = 31.0,
    x_half_radius: float = 0.37,
    y_half_radius: float = 0.25,
) -> tuple[np.ndarray, np.ndarray]:
    """Return longitude and latitude maps for a cropped orthographic view."""
    x = np.linspace(-x_half_radius, x_half_radius, width, dtype=np.float64)
    y = np.linspace(y_half_radius, -y_half_radius, height, dtype=np.float64)
    xx, yy = np.meshgrid(x, y)
    rho = np.sqrt(xx * xx + yy * yy)
    if float(rho.max()) >= 1.0:
        raise ValueError("Orthographic crop extends past the visible hemisphere")

    c = np.arcsin(rho)
    sin_c = np.sin(c)
    cos_c = np.cos(c)
    lat0 = math.radians(center_lat_deg)
    lon0 = math.radians(center_lon_deg)

    safe_rho = np.where(rho == 0.0, 1.0, rho)
    lat = np.arcsin(
        cos_c * math.sin(lat0)
        + (yy * sin_c * math.cos(lat0) / safe_rho)
    )
    lon = lon0 + np.arctan2(
        xx * sin_c,
        safe_rho * math.cos(lat0) * cos_c
        - yy * math.sin(lat0) * sin_c,
    )
    lat[rho == 0.0] = lat0
    lon[rho == 0.0] = lon0
    return np.degrees(lon).astype(np.float32), np.degrees(lat).astype(np.float32)


def build_background(
    bmng_path: Path, lon_map: np.ndarray, lat_map: np.ndarray
) -> np.ndarray:
    bmng_bgr = cv2.imread(str(bmng_path), cv2.IMREAD_COLOR)
    if bmng_bgr is None:
        raise FileNotFoundError(f"Could not read {bmng_path}")
    bmng = cv2.cvtColor(bmng_bgr, cv2.COLOR_BGR2RGB)
    source_h, source_w = bmng.shape[:2]
    map_x = ((lon_map + 180.0) / 360.0 * (source_w - 1)).astype(np.float32)
    map_y = ((90.0 - lat_map) / 180.0 * (source_h - 1)).astype(np.float32)
    background = cv2.remap(
        bmng,
        map_x,
        map_y,
        interpolation=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    # The released visualization uses a subdued Blue Marble treatment so the
    # translucent smoke remains legible.  This is a global tonal operation only.
    background = np.clip(background.astype(np.float32) * 0.90, 0, 255).astype(np.uint8)
    return background


def make_data_maps(
    lon_map: np.ndarray,
    lat_map: np.ndarray,
    data_lon: np.ndarray,
    data_lat: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if not (np.all(np.diff(data_lon) > 0) and np.all(np.diff(data_lat) > 0)):
        raise RuntimeError("Expected monotonically increasing GEOS longitude/latitude")
    map_x = ((lon_map - data_lon[0]) / (data_lon[1] - data_lon[0])).astype(np.float32)
    map_y = ((lat_map - data_lat[0]) / (data_lat[1] - data_lat[0])).astype(np.float32)
    return map_x, map_y


def extract_panels_and_palette(source_first: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    frame_bgr = cv2.imread(str(source_first), cv2.IMREAD_COLOR)
    if frame_bgr is None or frame_bgr.shape[:2] != (1080, 1920):
        raise RuntimeError(f"Expected a 1920x1080 NASA source frame at {source_first}")
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    legend = frame_rgb[0:190, 0:460].copy()
    logos = frame_rgb[0:190, 1730:1920].copy()

    # Exact sampled gradient from the legend released with SVS 5666.
    gradient = frame_rgb[103, 65:395].copy()
    palette = cv2.resize(
        gradient[np.newaxis, :, :], (256, 1), interpolation=cv2.INTER_LINEAR
    )[0]
    return legend, logos, palette


def colorize_smoke(
    background: np.ndarray,
    aod_native: np.ndarray,
    map_x: np.ndarray,
    map_y: np.ndarray,
    palette: np.ndarray,
) -> np.ndarray:
    # Mild native-grid smoothing suppresses block boundaries without changing
    # the broad transport features represented by the 0.25-degree product.
    smoothed = cv2.GaussianBlur(aod_native, (0, 0), sigmaX=0.55, sigmaY=0.55)
    aod = cv2.remap(
        smoothed,
        map_x,
        map_y,
        interpolation=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0.0,
    )
    aod = np.clip(aod, 0.0, AOD_LEGEND_MAX)
    normalized = aod / AOD_LEGEND_MAX
    indices = np.clip(np.rint(normalized * 255.0), 0, 255).astype(np.uint8)
    smoke_rgb = palette[indices].astype(np.float32)

    # Match the released visualization's transparent low-AOD haze and denser,
    # darker high-AOD plumes.  Values at or below 0.003 remain transparent.
    visible = np.clip((aod - 0.003) / (AOD_LEGEND_MAX - 0.003), 0.0, 1.0)
    alpha = (0.92 * np.power(visible, 0.55))[..., np.newaxis]
    composite = background.astype(np.float32) * (1.0 - alpha) + smoke_rgb * alpha
    return np.clip(composite, 0, 255).astype(np.uint8)


def ass_time(seconds: float) -> str:
    centiseconds = int(round(seconds * 100.0))
    hours, rem = divmod(centiseconds, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def write_timestamp_ass(path: Path, frame_times: list[datetime]) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Clock,DejaVu Sans,24,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for index, stamp in enumerate(frame_times):
        start = ass_time(index / FPS)
        end = ass_time((index + 1) / FPS)
        date_text = stamp.strftime("%d %b %Y %H:00 UTC")
        lines.append(
            f"Dialogue: 0,{start},{end},Clock,,0,0,0,,"
            r"{\an7\pos(44,998)\fs24\b1}GEOS-FP"
            "\n"
        )
        lines.append(
            f"Dialogue: 0,{start},{end},Clock,,0,0,0,,"
            rf"{{\an7\pos(44,1030)\fs25\b1}}{date_text}"
            "\n"
        )
    path.write_text("".join(lines), encoding="utf-8")


def make_clock_panel(path: Path) -> None:
    panel = Image.new("RGBA", (460, 110), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    # Rounded upper-right corner; the rectangle intentionally runs through the
    # bottom edge, matching the clock treatment in the released NASA render.
    draw.rounded_rectangle((0, 30, 441, 130), radius=20, fill=(2, 25, 34, 220))
    draw.rectangle((0, 50, 441, 110), fill=(2, 25, 34, 220))
    panel.save(path)


def hourly_times(start: datetime, end: datetime) -> list[datetime]:
    count = int((end - start).total_seconds() // 3600) + 1
    return [start + timedelta(hours=i) for i in range(count)]


def render_intermediate(
    data: np.ndarray,
    source_times: list[datetime],
    source_index_offset: int,
    frame_times: list[datetime],
    background: np.ndarray,
    data_map_x: np.ndarray,
    data_map_y: np.ndarray,
    palette: np.ndarray,
) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{LOW_WIDTH}x{LOW_HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "17",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        str(INTERMEDIATE),
    ]
    print("+", " ".join(cmd), flush=True)
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert process.stdin is not None

    data_start = source_times[source_index_offset]
    step_seconds = (source_times[source_index_offset + 1] - data_start).total_seconds()
    if not (10_790 <= step_seconds <= 10_810):
        raise RuntimeError(f"Unexpected source time step {step_seconds} seconds")

    started = time.monotonic()
    try:
        for frame_index, stamp in enumerate(frame_times):
            position = (stamp - data_start).total_seconds() / step_seconds
            lower = int(math.floor(position))
            fraction = float(position - lower)
            lower = max(0, min(lower, data.shape[0] - 2))
            upper = lower + 1
            field = data[lower] * (1.0 - fraction) + data[upper] * fraction
            frame = colorize_smoke(background, field, data_map_x, data_map_y, palette)
            process.stdin.write(np.ascontiguousarray(frame).tobytes())
            if frame_index % 120 == 0 or frame_index == len(frame_times) - 1:
                elapsed = time.monotonic() - started
                print(
                    f"Rendered frame {frame_index + 1}/{len(frame_times)} "
                    f"({stamp.isoformat()}); elapsed {elapsed:.1f}s",
                    flush=True,
                )
    finally:
        process.stdin.close()
    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, cmd)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    if not SOURCE_FIRST.exists():
        raise FileNotFoundError(SOURCE_FIRST)
    if not BMNG_FILE.exists():
        raise FileNotFoundError(BMNG_FILE)

    print(f"Opening {GEOS_URL}", flush=True)
    dataset = open_url(GEOS_URL, protocol="dap2")
    all_times, raw_time, time_attrs = decode_time_axis(dataset["time"])
    lat = np.asarray(dataset["lat"][:].data, dtype=np.float32).reshape(-1)
    lon = np.asarray(dataset["lon"][:].data, dtype=np.float32).reshape(-1)

    source_i0, source_i1 = nearest_bracketing_indices(
        all_times, REQUEST_START, REQUEST_END
    )
    # Expand by one time step on both sides when possible, preserving stable
    # interpolation at the requested endpoints.
    source_i0 = max(0, source_i0 - 1)
    source_i1 = min(len(all_times) - 1, source_i1 + 1)

    lat_mask = np.flatnonzero((lat >= 8.0) & (lat <= 53.0))
    lon_mask = np.flatnonzero((lon >= -28.0) & (lon <= 48.0))
    if not lat_mask.size or not lon_mask.size:
        raise RuntimeError("GEOS coordinate subset is empty")
    y0, y1 = int(lat_mask[0]), int(lat_mask[-1]) + 1
    x0, x1 = int(lon_mask[0]), int(lon_mask[-1]) + 1

    data = fetch_data_chunks(
        dataset[VARIABLE], source_i0, source_i1, y0, y1, x0, x1
    )
    data_times = all_times[source_i0 : source_i1 + 1]
    data_lat = lat[y0:y1]
    data_lon = lon[x0:x1]

    np.savez_compressed(
        DATA_FILE,
        brexttau=data,
        time_iso=np.asarray([item.isoformat() for item in data_times]),
        lat=data_lat,
        lon=data_lon,
        source_url=np.asarray(GEOS_URL),
        variable=np.asarray(VARIABLE),
    )

    lon_map, lat_map = inverse_orthographic_maps(LOW_WIDTH, LOW_HEIGHT)
    background = build_background(BMNG_FILE, lon_map, lat_map)
    data_map_x, data_map_y = make_data_maps(lon_map, lat_map, data_lon, data_lat)
    legend, logos, palette = extract_panels_and_palette(SOURCE_FIRST)

    Image.fromarray(legend).save(BUILD / "legend-panel.png")
    Image.fromarray(logos).save(BUILD / "logo-panel.png")
    make_clock_panel(BUILD / "clock-panel.png")

    frame_times = hourly_times(REQUEST_START, REQUEST_END)
    render_intermediate(
        data,
        all_times,
        source_i0,
        frame_times,
        background,
        data_map_x,
        data_map_y,
        palette,
    )

    ass_file = BUILD / "timestamps.ass"
    write_timestamp_ass(ass_file, frame_times)

    filter_complex = (
        "[0:v]scale=1920:1080:flags=lanczos[bg];"
        "[bg][1:v]overlay=0:0[tmp1];"
        "[tmp1][2:v]overlay=W-w:0[tmp2];"
        "[tmp2][3:v]overlay=0:H-h[tmp3];"
        "[tmp3]ass=build/timestamps.ass,format=yuv420p[outv]"
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(INTERMEDIATE),
            "-loop",
            "1",
            "-i",
            str(BUILD / "legend-panel.png"),
            "-loop",
            "1",
            "-i",
            str(BUILD / "logo-panel.png"),
            "-loop",
            "1",
            "-i",
            str(BUILD / "clock-panel.png"),
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
            "-frames:v",
            str(len(frame_times)),
            "-r",
            str(FPS),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "17",
            "-profile:v",
            "high",
            "-level",
            "4.2",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(FINAL_VIDEO),
        ]
    )

    duration = len(frame_times) / FPS
    qc_times = {
        "qc-start.png": 0.0,
        "qc-midpoint.png": duration / 2.0,
        "qc-end.png": max(0.0, duration - 1.0 / FPS),
    }
    for filename, second in qc_times.items():
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{second:.6f}",
                "-i",
                str(FINAL_VIDEO),
                "-frames:v",
                "1",
                str(BUILD / filename),
            ]
        )

    metadata = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "video": FINAL_VIDEO.name,
        "sha256": sha256(FINAL_VIDEO),
        "width": FINAL_WIDTH,
        "height": FINAL_HEIGHT,
        "fps": FPS,
        "frame_count": len(frame_times),
        "duration_seconds": duration,
        "first_frame_utc": frame_times[0].isoformat(),
        "last_frame_utc": frame_times[-1].isoformat(),
        "one_modeled_hour_per_video_frame": True,
        "data_source": GEOS_URL,
        "variable": VARIABLE,
        "variable_description": "Brown carbon aerosol optical depth",
        "native_spatial_grid": {
            "latitude_spacing_degrees": float(data_lat[1] - data_lat[0]),
            "longitude_spacing_degrees": float(data_lon[1] - data_lon[0]),
            "latitude_range": [float(data_lat[0]), float(data_lat[-1])],
            "longitude_range": [float(data_lon[0]), float(data_lon[-1])],
        },
        "native_time_range": [data_times[0].isoformat(), data_times[-1].isoformat()],
        "native_time_step_hours": 3,
        "temporal_resampling": "linear interpolation to integer UTC hours",
        "legend_range": [0.0, AOD_LEGEND_MAX],
        "geos_time_attributes": {key: str(value) for key, value in time_attrs.items()},
        "geos_raw_time_first": float(raw_time[0]),
        "geos_raw_time_last": float(raw_time[-1]),
        "data_shape": list(data.shape),
        "data_min": float(data.min()),
        "data_max": float(data.max()),
        "view": {
            "projection": "orthographic",
            "center_lon_deg": 10.0,
            "center_lat_deg": 31.0,
            "x_half_earth_radius": 0.37,
            "y_half_earth_radius": 0.25,
        },
    }
    (BUILD / "RENDER_METADATA.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, indent=2), flush=True)


if __name__ == "__main__":
    main()
