#!/usr/bin/env python3
"""Render the NASA-SVS-style northern-Algeria wildfire-smoke continuation.

The released NASA SVS 5666 movie ends at 2026-07-30 00:00 UTC. This script
uses the public NASA GEOS-FP Brown Carbon AOD field (BREXTTAU) for later
frames, retaining the released movie's crop, information-panel geometry,
linear 0--0.5 legend, and one-hour-per-frame pacing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
import pandas as pd
import shapefile
import xarray as xr
from PIL import Image, ImageDraw, ImageFont

MAP_W = 960
MAP_H = 540
OUT_W = 1920
OUT_H = 1080
FPS = 30

OPENDAP_URL = (
    "https://opendap.nccs.nasa.gov/dods/GEOS-5/fp/0.25_deg/assim/"
    "tavg3_2d_aer_Nx"
)

# The BMNG candidate image is an equirectangular Plate Carree window. The
# affine was calibrated by enhanced-correlation registration against the exact
# 960x540 map crop in the released NASA movie. It maps output-map pixels to
# candidate-window pixels and is therefore used with WARP_INVERSE_MAP.
CANDIDATE_EXTENT = (-25.0, 40.0, 18.0, 55.0)
OUTPUT_TO_CANDIDATE = np.array(
    [
        [0.65501857, 0.10576969, 131.08880],
        [-0.04880585, 0.56306636, 139.45094],
    ],
    dtype=np.float32,
)

# Robust linear color match from NASA's BMNG July source image to the released
# SVS crop. Input and output are OpenCV BGR triplets.
BASE_COLOR_MATRIX = np.array(
    [
        [0.72689277, 0.07236470, -0.35863242],
        [-0.49824062, 0.36469924, 0.50070554],
        [0.32761851, 0.14082676, 0.51403910],
        [96.40364838, 101.37567902, 87.85977936],
    ],
    dtype=np.float32,
)

# Smoke hue anchors visually matched to the released linear 0--0.5 legend.
# RGB values are un-premultiplied display colors; density-dependent alpha is
# applied separately so zero AOD remains transparent.
SMOKE_VALUES = np.array(
    [0.0, 0.01, 0.03, 0.06, 0.10, 0.18, 0.25, 0.35, 0.40, 0.50],
    dtype=np.float32,
)
SMOKE_RGB = np.array(
    [
        [250, 250, 248],
        [247, 243, 235],
        [242, 229, 211],
        [236, 207, 178],
        [231, 181, 145],
        [224, 139, 96],
        [207, 102, 66],
        [169, 65, 43],
        [137, 47, 33],
        [74, 31, 42],
    ],
    dtype=np.float32,
)

FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
)


@dataclass(frozen=True)
class Panels:
    legend: np.ndarray
    logos: np.ndarray
    clock_background: np.ndarray


def parse_utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def font_path() -> str:
    for candidate in FONT_BOLD_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("No supported bold sans-serif font found")


def frame_from_video(path: Path, frame_number: int = 0) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {path}")
    if frame_number:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"Could not read frame {frame_number} from {path}")
    return frame


def extract_panels(source_movie: Path) -> Panels:
    frame = frame_from_video(source_movie, 0)
    if frame.shape[:2] != (1080, 1920):
        raise RuntimeError(f"Unexpected source dimensions: {frame.shape}")

    legend = frame[0:190, 0:460].copy()
    logos = frame[0:190, 1730:1920].copy()
    clock = frame[970:1080, 0:460].copy()

    # Preserve the released panel's diagonal edge and map texture, but replace
    # its changing text area with a clean rounded dark-glass panel.
    pil = Image.fromarray(cv2.cvtColor(clock, cv2.COLOR_BGR2RGB)).convert("RGBA")
    overlay = Image.new("RGBA", pil.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle((31, 19, 443, 116), radius=20, fill=(4, 24, 35, 232))
    pil = Image.alpha_composite(pil, overlay).convert("RGB")
    clock_background = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return Panels(legend=legend, logos=logos, clock_background=clock_background)


def crop_bmng_candidate(bmng_bgr: np.ndarray) -> np.ndarray:
    h, w = bmng_bgr.shape[:2]
    lon0, lon1, lat0, lat1 = CANDIDATE_EXTENT
    x0 = int((lon0 + 180.0) / 360.0 * w)
    x1 = int((lon1 + 180.0) / 360.0 * w)
    y0 = int((90.0 - lat1) / 180.0 * h)
    y1 = int((90.0 - lat0) / 180.0 * h)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise RuntimeError("BMNG crop is outside the image")
    return cv2.resize(
        bmng_bgr[y0:y1, x0:x1],
        (MAP_W, MAP_H),
        interpolation=cv2.INTER_LANCZOS4,
    )


def color_match_base(base_bgr: np.ndarray) -> np.ndarray:
    f = base_bgr.astype(np.float32)
    augmented = np.concatenate(
        [f, np.ones((f.shape[0], f.shape[1], 1), dtype=np.float32)], axis=2
    )
    return np.clip(augmented @ BASE_COLOR_MATRIX, 0, 255).astype(np.uint8)


def geographic_to_output(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon0, lon1, lat0, lat1 = CANDIDATE_EXTENT
    u = (lon - lon0) / (lon1 - lon0) * (MAP_W - 1)
    v = (lat1 - lat) / (lat1 - lat0) * (MAP_H - 1)
    affine3 = np.vstack([OUTPUT_TO_CANDIDATE, [0.0, 0.0, 1.0]])
    inv = np.linalg.inv(affine3)
    pts = np.stack([u, v, np.ones_like(u)], axis=-1)
    out = pts @ inv.T
    return out[..., 0], out[..., 1]


def draw_dashed_segment(
    canvas: np.ndarray,
    p0: tuple[float, float],
    p1: tuple[float, float],
    color: tuple[int, int, int],
    dash: float = 3.2,
    gap: float = 3.2,
) -> None:
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length < 0.4 or length > 120.0:
        return
    ux, uy = dx / length, dy / length
    cursor = 0.0
    while cursor < length:
        end = min(cursor + dash, length)
        a = (int(round(x0 + ux * cursor)), int(round(y0 + uy * cursor)))
        b = (int(round(x0 + ux * end)), int(round(y0 + uy * end)))
        cv2.line(canvas, a, b, color, 1, cv2.LINE_AA)
        cursor += dash + gap


def add_country_boundaries(base_bgr: np.ndarray, shp_path: Path | None) -> np.ndarray:
    if shp_path is None:
        return base_bgr
    if not shp_path.is_file():
        raise FileNotFoundError(shp_path)

    lines = np.zeros_like(base_bgr)
    reader = shapefile.Reader(str(shp_path))
    lon_min, lon_max, lat_min, lat_max = (-30.0, 45.0, 14.0, 58.0)

    for shape in reader.shapes():
        points = np.asarray(shape.points, dtype=np.float64)
        if points.size == 0:
            continue
        parts = list(shape.parts) + [len(points)]
        for start, stop in zip(parts[:-1], parts[1:]):
            part = points[start:stop]
            if len(part) < 2:
                continue
            if (
                part[:, 0].max() < lon_min
                or part[:, 0].min() > lon_max
                or part[:, 1].max() < lat_min
                or part[:, 1].min() > lat_max
            ):
                continue
            xs, ys = geographic_to_output(part[:, 0], part[:, 1])
            for a, b in zip(zip(xs[:-1], ys[:-1]), zip(xs[1:], ys[1:])):
                if (
                    -5 <= a[0] <= MAP_W + 5
                    and -5 <= a[1] <= MAP_H + 5
                    and -5 <= b[0] <= MAP_W + 5
                    and -5 <= b[1] <= MAP_H + 5
                ):
                    draw_dashed_segment(lines, a, b, (48, 48, 48))

    mask = np.max(lines, axis=2) > 0
    out = base_bgr.copy()
    if np.any(mask):
        out[mask] = np.clip(
            0.58 * out[mask].astype(np.float32)
            + 0.42 * lines[mask].astype(np.float32),
            0,
            255,
        ).astype(np.uint8)
    return out


def build_base_map(bmng_path: Path, boundaries_shp: Path | None) -> np.ndarray:
    bmng = cv2.imread(str(bmng_path), cv2.IMREAD_COLOR)
    if bmng is None:
        raise RuntimeError(f"Could not read BMNG image: {bmng_path}")
    candidate = crop_bmng_candidate(bmng)
    aligned = cv2.warpAffine(
        candidate,
        OUTPUT_TO_CANDIDATE,
        (MAP_W, MAP_H),
        flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    aligned = color_match_base(aligned)
    return add_country_boundaries(aligned, boundaries_shp)


def output_lon_lat_grid() -> tuple[np.ndarray, np.ndarray]:
    yy, xx = np.mgrid[0:MAP_H, 0:MAP_W].astype(np.float32)
    u = (
        OUTPUT_TO_CANDIDATE[0, 0] * xx
        + OUTPUT_TO_CANDIDATE[0, 1] * yy
        + OUTPUT_TO_CANDIDATE[0, 2]
    )
    v = (
        OUTPUT_TO_CANDIDATE[1, 0] * xx
        + OUTPUT_TO_CANDIDATE[1, 1] * yy
        + OUTPUT_TO_CANDIDATE[1, 2]
    )
    lon0, lon1, lat0, lat1 = CANDIDATE_EXTENT
    lon = lon0 + u / (MAP_W - 1) * (lon1 - lon0)
    lat = lat1 - v / (MAP_H - 1) * (lat1 - lat0)
    return lon, lat


def load_brown_carbon_aod(
    start: pd.Timestamp, end: pd.Timestamp
) -> tuple[np.ndarray, np.ndarray, np.ndarray, pd.DatetimeIndex, dict]:
    request_start = start - pd.Timedelta(hours=3)
    request_end = end + pd.Timedelta(hours=3)
    ds = xr.open_dataset(OPENDAP_URL, engine="pydap", decode_times=True)
    try:
        da = ds["brexttau"].sel(
            time=slice(request_start.tz_localize(None), request_end.tz_localize(None)),
            lat=slice(14.0, 58.0),
            lon=slice(-30.0, 45.0),
        )
        da = da.load()
    finally:
        ds.close()

    if da.sizes.get("time", 0) < 2:
        raise RuntimeError("NASA OPeNDAP returned too few time steps")
    hourly = pd.date_range(start=start, end=end, freq="1h", tz="UTC")
    target_naive = hourly.tz_localize(None)
    interp = da.interp(time=target_naive).astype("float32")
    values = np.asarray(interp.values, dtype=np.float32)
    if not np.isfinite(values).all():
        raise RuntimeError("Interpolated BREXTTAU contains missing values")

    metadata = {
        "opendap_url": OPENDAP_URL,
        "variable": "brexttau",
        "variable_attributes": dict(da.attrs),
        "source_time_first": str(pd.Timestamp(da.time.values[0])),
        "source_time_last": str(pd.Timestamp(da.time.values[-1])),
        "source_time_count": int(da.sizes["time"]),
        "source_lat_first": float(da.lat.values[0]),
        "source_lat_last": float(da.lat.values[-1]),
        "source_lon_first": float(da.lon.values[0]),
        "source_lon_last": float(da.lon.values[-1]),
        "hourly_start": hourly[0].isoformat(),
        "hourly_end": hourly[-1].isoformat(),
        "hourly_count": len(hourly),
        "temporal_processing": (
            "Linear interpolation from the public 3-hour-centered GEOS-FP "
            "time-averaged BREXTTAU fields to the hourly animation timestamps."
        ),
        "minimum": float(np.nanmin(values)),
        "maximum": float(np.nanmax(values)),
    }
    return values, np.asarray(da.lat.values), np.asarray(da.lon.values), hourly, metadata


def remap_indices(data_lats: np.ndarray, data_lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon_grid, lat_grid = output_lon_lat_grid()
    lon_step = float(data_lons[1] - data_lons[0])
    lat_step = float(data_lats[1] - data_lats[0])
    map_x = ((lon_grid - float(data_lons[0])) / lon_step).astype(np.float32)
    map_y = ((lat_grid - float(data_lats[0])) / lat_step).astype(np.float32)
    return map_x, map_y


def smoke_colors_and_alpha(field: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    clean = np.clip(field, 0.0, 0.5).astype(np.float32)
    smooth = cv2.GaussianBlur(clean, (0, 0), sigmaX=0.72, sigmaY=0.72)

    rgb = np.empty((*smooth.shape, 3), dtype=np.float32)
    for channel in range(3):
        rgb[..., channel] = np.interp(
            smooth, SMOKE_VALUES, SMOKE_RGB[:, channel]
        )

    effective = np.maximum(smooth - 0.0015, 0.0)
    alpha = 0.93 * (1.0 - np.exp(-effective / 0.038))
    alpha = np.clip(alpha, 0.0, 0.93).astype(np.float32)
    return rgb, alpha


def composite_smoke(base_bgr: np.ndarray, field: np.ndarray) -> np.ndarray:
    rgb, alpha = smoke_colors_and_alpha(field)
    smoke_bgr = rgb[..., ::-1]
    a = alpha[..., None]
    out = base_bgr.astype(np.float32) * (1.0 - a) + smoke_bgr * a
    return np.clip(out, 0, 255).astype(np.uint8)


def make_clock_panel(background_bgr: np.ndarray, timestamp: pd.Timestamp) -> np.ndarray:
    pil = Image.fromarray(cv2.cvtColor(background_bgr, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = ImageFont.truetype(font_path(), 24)
    draw.text((44, 26), "GEOS-FP Analysis", font=font, fill=(255, 255, 255))
    draw.text(
        (44, 57),
        timestamp.strftime("%d %b %Y %H:%M UTC"),
        font=font,
        fill=(255, 255, 255),
    )
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def compose_full_frame(
    map_bgr: np.ndarray, panels: Panels, timestamp: pd.Timestamp
) -> np.ndarray:
    enlarged = cv2.resize(
        map_bgr, (OUT_W, OUT_H), interpolation=cv2.INTER_LANCZOS4
    )
    enlarged[0:190, 0:460] = panels.legend
    enlarged[0:190, 1730:1920] = panels.logos
    enlarged[970:1080, 0:460] = make_clock_panel(
        panels.clock_background, timestamp
    )
    return enlarged


def map_field_to_output(
    field: np.ndarray, map_x: np.ndarray, map_y: np.ndarray
) -> np.ndarray:
    return cv2.remap(
        field.astype(np.float32),
        map_x,
        map_y,
        interpolation=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0.0,
    )


def ffmpeg_writer(output_path: Path) -> subprocess.Popen:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s:v", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-i", "-", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-profile:v", "high", "-level:v", "4.2", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output_path),
    ]
    return subprocess.Popen(command, stdin=subprocess.PIPE)


def render_frames(
    values: np.ndarray,
    times: pd.DatetimeIndex,
    lats: np.ndarray,
    lons: np.ndarray,
    base: np.ndarray,
    panels: Panels,
    output_path: Path | None,
    selected_indices: Iterable[int] | None = None,
    frame_dir: Path | None = None,
) -> None:
    map_x, map_y = remap_indices(lats, lons)
    selected = set(selected_indices or [])
    writer = ffmpeg_writer(output_path) if output_path else None
    try:
        for i, (raw, timestamp) in enumerate(zip(values, times)):
            if writer is None and i not in selected:
                continue
            mapped = map_field_to_output(raw, map_x, map_y)
            smoke_map = composite_smoke(base, mapped)
            full = compose_full_frame(smoke_map, panels, timestamp)
            if writer is not None:
                assert writer.stdin is not None
                writer.stdin.write(full.tobytes())
            if frame_dir is not None and i in selected:
                cv2.imwrite(
                    str(frame_dir / f"generated-{timestamp:%Y%m%d-%H%M}.png"), full
                )
            if i % 120 == 0:
                print(f"rendered {i + 1}/{len(times)}: {timestamp.isoformat()}", flush=True)
    finally:
        if writer is not None:
            assert writer.stdin is not None
            writer.stdin.close()
            rc = writer.wait()
            if rc != 0:
                raise RuntimeError(f"ffmpeg exited with status {rc}")


def save_reference_frames(source_movie: Path, output_dir: Path) -> None:
    # The released movie has a single 12-hour source-data gap after 11 Jul
    # 00:00; these are the exact released frames for the labeled timestamps.
    references = {
        "20260701-0000": 0,
        "20260714-0000": 300,
        "20260730-0000": 684,
    }
    for stamp, frame_number in references.items():
        frame = frame_from_video(source_movie, frame_number)
        focus = cv2.resize(
            frame[540:1080, 460:1420],
            (OUT_W, OUT_H),
            interpolation=cv2.INTER_LANCZOS4,
        )
        focus[0:190, 0:460] = frame[0:190, 0:460]
        focus[0:190, 1730:1920] = frame[0:190, 1730:1920]
        focus[970:1080, 0:460] = frame[970:1080, 0:460]
        cv2.imwrite(str(output_dir / f"reference-{stamp}.png"), focus)


def calibration_indices(times: pd.DatetimeIndex) -> list[int]:
    targets = [
        pd.Timestamp("2026-07-01 00:00", tz="UTC"),
        pd.Timestamp("2026-07-14 00:00", tz="UTC"),
        pd.Timestamp("2026-07-30 00:00", tz="UTC"),
        pd.Timestamp("2026-08-15 12:00", tz="UTC"),
        pd.Timestamp("2026-09-01 00:00", tz="UTC"),
    ]
    return [int(times.get_indexer([target])[0]) for target in targets]


def make_comparisons(directory: Path) -> None:
    for stamp in ("20260701-0000", "20260714-0000", "20260730-0000"):
        ref_path = directory / f"reference-{stamp}.png"
        gen_path = directory / f"generated-{stamp}.png"
        if not (ref_path.is_file() and gen_path.is_file()):
            continue
        ref = cv2.imread(str(ref_path))
        gen = cv2.imread(str(gen_path))
        comparison = np.concatenate([ref, gen], axis=1)
        cv2.putText(
            comparison, "Released NASA SVS 5666", (30, 1040),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA,
        )
        cv2.putText(
            comparison, "Public GEOS-FP reconstruction", (OUT_W + 30, 1040),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA,
        )
        cv2.imwrite(str(directory / f"comparison-{stamp}.jpg"), comparison)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_provenance(output_dir: Path, metadata: dict) -> None:
    text = f"""Northern Algeria wildfire-smoke animation, 1 July–1 September 2026

Released NASA segment
---------------------
The 1 July 2026 00:00 UTC through 30 July 2026 00:00 UTC segment is a
spatially reframed copy of the official NASA Scientific Visualization Studio
item 5666 movie. Its scientific pixels, legend, NASA/GMAO panel, timestamps,
and temporal sequence are retained. The released sequence contains a
12-hour timestamp jump after 11 July 00:00 UTC.

Continuation
------------
The 30 July 2026 01:00 UTC through 1 September 2026 00:00 UTC continuation is
constructed from NASA's public GEOS-FP 0.25-degree assimilation archive,
collection tavg3_2d_aer_Nx, variable BREXTTAU (Brown Carbon aerosol optical
depth). The public fields are 3-hour-centered time averages. They are
linearly interpolated to hourly timestamps solely to match the released
movie's one-hour-per-frame pacing. No extrapolation is used.

The continuation uses the released movie's exact northern-Algeria camera crop,
information-panel sizes and positions, 1920x1080 canvas, 30 fps pacing, and
linear 0–0.5 smoke legend. The Blue Marble: Next Generation July image is
registered to the released camera, then the BREXTTAU field is resampled into
that camera and composited with the matching smoke palette. Country boundaries
are from Natural Earth.

Qualification
-------------
NASA has not published the GEOS-CAM 2-km replay files or the visualizers'
complete internal production project for dates after the released movie.
Consequently, the post-30-July segment is the closest reproducible,
authoritative public-data continuation; it is not represented as a bit-exact
rerender of the unpublished GEOS-CAM workflow.

Data metadata
-------------
{json.dumps(metadata, indent=2, sort_keys=True)}
"""
    (output_dir / "PROVENANCE.txt").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-movie", type=Path, required=True)
    parser.add_argument("--bmng", type=Path, required=True)
    parser.add_argument("--boundaries-shp", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("calibration", "continuation"), default="calibration"
    )
    parser.add_argument("--start", default="2026-07-01 00:00 UTC")
    parser.add_argument("--end", default="2026-09-01 00:00 UTC")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if end < start:
        raise ValueError("end must not precede start")

    print("building registered basemap", flush=True)
    base = build_base_map(args.bmng, args.boundaries_shp)
    cv2.imwrite(str(args.output_dir / "registered-basemap.png"), base)
    panels = extract_panels(args.source_movie)
    cv2.imwrite(str(args.output_dir / "legend-panel.png"), panels.legend)
    cv2.imwrite(str(args.output_dir / "logos-panel.png"), panels.logos)

    print("loading NASA GEOS-FP BREXTTAU", flush=True)
    values, lats, lons, times, metadata = load_brown_carbon_aod(start, end)
    (args.output_dir / "DATA-METADATA.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
    )

    if args.mode == "calibration":
        indices = calibration_indices(times)
        save_reference_frames(args.source_movie, args.output_dir)
        render_frames(
            values, times, lats, lons, base, panels,
            output_path=None, selected_indices=indices, frame_dir=args.output_dir,
        )
        make_comparisons(args.output_dir)
    else:
        continuation = args.output_dir / "continuation-geos-fp.mp4"
        render_frames(
            values, times, lats, lons, base, panels, output_path=continuation
        )
        metadata["continuation_file"] = continuation.name
        metadata["continuation_sha256"] = sha256(continuation)
        metadata["continuation_frames"] = len(times)
        metadata["continuation_fps"] = FPS
        write_provenance(args.output_dir, metadata)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
