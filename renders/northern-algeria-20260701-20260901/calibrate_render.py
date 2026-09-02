#!/usr/bin/env python3
"""Calibrate a northern-Algeria GEOS-FP smoke rendering against NASA SVS 5666."""
from __future__ import annotations

from pathlib import Path
import math

import cv2
import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter, map_coordinates
import xarray as xr

SOURCE_VIDEO = Path("build/source.mp4")
OUT = Path("build/calibration")
OPENDAP = "https://opendap.nccs.nasa.gov/dods/GEOS-5/fp/0.25_deg/assim/tavg3_2d_aer_Nx"

LON0 = math.radians(12.39475965)
LAT0 = math.radians(45.26790279)
ROLL = math.radians(0.20332813)
CAMERA_DISTANCE = 15.10562714
GLOBE_RADIUS_PX = 933.3019089568
CENTER_FULL_X = 954.5029239655
CENTER_FULL_Y = 610.5516449753
CROP_X = 460
CROP_Y = 540
CROP_W = 960
CROP_H = 540
RESIDUAL_AFFINE = np.array(
    [
        [1.12697571, -0.04759355, 13.64303199],
        [0.04622319, 1.10706808, -35.16372547],
        [0.0, 0.0, 1.0],
    ],
    dtype=np.float64,
)


def read_source_frame(index: int) -> np.ndarray:
    cap = cv2.VideoCapture(str(SOURCE_VIDEO))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {SOURCE_VIDEO}")
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"Could not decode source frame {index}")
    return frame


def output_lon_lat(width: int = CROP_W, height: int = CROP_H) -> tuple[np.ndarray, np.ndarray]:
    """Map exact output-crop pixels back to geodetic longitude/latitude."""
    sx = CROP_W / width
    sy = CROP_H / height
    x = (np.arange(width, dtype=np.float64) + 0.5) * sx - 0.5
    y = (np.arange(height, dtype=np.float64) + 0.5) * sy - 0.5
    xx, yy = np.meshgrid(x, y)

    inv_affine = np.linalg.inv(RESIDUAL_AFFINE)
    hom = np.stack((xx.ravel(), yy.ravel(), np.ones(xx.size, dtype=np.float64)))
    candidate = inv_affine @ hom
    u = candidate[0].reshape(height, width)
    v = candidate[1].reshape(height, width)

    cx = CENTER_FULL_X - CROP_X
    cy = CENTER_FULL_Y - CROP_Y
    k = GLOBE_RADIUS_PX * math.sqrt(CAMERA_DISTANCE**2 - 1.0)
    px = (u - cx) / k
    py = -(v - cy) / k

    aq = 1.0 + px * px + py * py
    disc = 1.0 - (CAMERA_DISTANCE**2 - 1.0) * (px * px + py * py)
    if float(np.nanmin(disc)) < -1e-7:
        raise RuntimeError(f"Output map extends beyond globe: min discriminant={disc.min()}")
    t = (CAMERA_DISTANCE - np.sqrt(np.maximum(disc, 0.0))) / aq
    view_x = px * t
    view_y = py * t
    view_z = CAMERA_DISTANCE - t

    cr, sr = math.cos(ROLL), math.sin(ROLL)
    xr = view_x * cr + view_y * sr
    yr = -view_x * sr + view_y * cr
    zr = view_z

    cl, sl = math.cos(LON0), math.sin(LON0)
    cp, sp = math.cos(LAT0), math.sin(LAT0)
    east = np.array([-sl, cl, 0.0])
    north = np.array([-sp * cl, -sp * sl, cp])
    front = np.array([cp * cl, cp * sl, sp])

    gx = xr * east[0] + yr * north[0] + zr * front[0]
    gy = xr * east[1] + yr * north[1] + zr * front[1]
    gz = xr * east[2] + yr * north[2] + zr * front[2]

    lon = np.degrees(np.arctan2(gy, gx)).astype(np.float32)
    lat = np.degrees(np.arcsin(np.clip(gz, -1.0, 1.0))).astype(np.float32)
    return lon, lat


def read_gradient(source_first: np.ndarray) -> np.ndarray:
    """Sample the exact NASA legend ramp (BGR) from the released movie."""
    rows = source_first[96:116, 63:391].astype(np.float32)
    gradient = np.median(rows, axis=0)
    gradient = gaussian_filter(gradient, sigma=(1.2, 0.0), mode="nearest")
    return np.clip(gradient, 0, 255).astype(np.uint8)


def colorize(aod: np.ndarray, gradient: np.ndarray, tau: float, max_alpha: float) -> tuple[np.ndarray, np.ndarray]:
    norm = np.clip(aod / 0.5, 0.0, 1.0)
    position = norm * (len(gradient) - 1)
    lo = np.floor(position).astype(np.int32)
    hi = np.minimum(lo + 1, len(gradient) - 1)
    frac = (position - lo)[..., None]
    color = gradient[lo].astype(np.float32) * (1.0 - frac) + gradient[hi].astype(np.float32) * frac

    signal = np.maximum(aod - 0.001, 0.0)
    alpha = max_alpha * (1.0 - np.exp(-signal / tau))
    alpha = np.clip(alpha, 0.0, max_alpha).astype(np.float32)
    return color, alpha


def composite(base: np.ndarray, aod: np.ndarray, gradient: np.ndarray, tau: float, max_alpha: float) -> np.ndarray:
    color, alpha = colorize(aod, gradient, tau=tau, max_alpha=max_alpha)
    out = base.astype(np.float32) * (1.0 - alpha[..., None]) + color * alpha[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def interpolate_field(ds: xr.Dataset, when: pd.Timestamp) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lo = when - pd.Timedelta(hours=2)
    hi = when + pd.Timedelta(hours=2)
    da = ds["brexttau"].sel(
        time=slice(lo.to_datetime64(), hi.to_datetime64()),
        lat=slice(8.0, 55.0),
        lon=slice(-35.0, 48.0),
    ).load()
    if da.sizes.get("time", 0) < 2:
        raise RuntimeError(f"Insufficient bracketing data for {when}: {da.time.values}")
    interpolated = da.interp(time=when.to_datetime64())
    return (
        np.asarray(interpolated.values, dtype=np.float32),
        np.asarray(interpolated.lat.values, dtype=np.float32),
        np.asarray(interpolated.lon.values, dtype=np.float32),
    )


def sample_to_output(field: np.ndarray, lats: np.ndarray, lons: np.ndarray, out_lon: np.ndarray, out_lat: np.ndarray) -> np.ndarray:
    lat_step = float(lats[1] - lats[0])
    lon_step = float(lons[1] - lons[0])
    lat_idx = (out_lat - float(lats[0])) / lat_step
    lon_idx = (out_lon - float(lons[0])) / lon_step
    sampled = map_coordinates(
        field,
        [lat_idx, lon_idx],
        order=1,
        mode="nearest",
        prefilter=False,
    )
    return gaussian_filter(sampled, sigma=0.7, mode="nearest").astype(np.float32)


def montage(images: list[np.ndarray], labels: list[str], columns: int = 3) -> np.ndarray:
    if len(images) != len(labels):
        raise ValueError("images/labels length mismatch")
    tile_h, tile_w = images[0].shape[:2]
    label_h = 42
    rows = math.ceil(len(images) / columns)
    canvas = np.zeros((rows * (tile_h + label_h), columns * tile_w, 3), dtype=np.uint8)
    for i, (im, label) in enumerate(zip(images, labels)):
        r, c = divmod(i, columns)
        y = r * (tile_h + label_h)
        x = c * tile_w
        canvas[y : y + tile_h, x : x + tile_w] = im
        cv2.putText(
            canvas,
            label,
            (x + 12, y + tile_h + 29),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (240, 240, 240),
            2,
            cv2.LINE_AA,
        )
    return canvas


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    source_first = read_source_frame(0)
    base = source_first[CROP_Y : CROP_Y + CROP_H, CROP_X : CROP_X + CROP_W].copy()
    gradient = read_gradient(source_first)
    cv2.imwrite(str(OUT / "base-exact-source-frame-0.png"), base)

    grad_img = np.repeat(gradient[None, :, :], 50, axis=0)
    cv2.imwrite(str(OUT / "legend-gradient-sampled.png"), grad_img)

    out_lon, out_lat = output_lon_lat()
    np.savez_compressed(OUT / "camera-grid.npz", lon=out_lon, lat=out_lat)
    print("camera lon range", float(out_lon.min()), float(out_lon.max()))
    print("camera lat range", float(out_lat.min()), float(out_lat.max()))

    print("opening", OPENDAP)
    ds = xr.open_dataset(OPENDAP, engine="pydap", decode_times=True)
    targets = [
        (pd.Timestamp("2026-07-14T00:00:00"), 300),
        (pd.Timestamp("2026-07-30T00:00:00"), 684),
        (pd.Timestamp("2026-08-15T00:00:00"), None),
        (pd.Timestamp("2026-09-01T00:00:00"), None),
    ]
    variants = [
        (0.020, 0.82),
        (0.035, 0.88),
        (0.050, 0.92),
        (0.075, 0.95),
        (0.100, 0.95),
    ]

    for when, source_index in targets:
        field, lats, lons = interpolate_field(ds, when)
        aod = sample_to_output(field, lats, lons, out_lon, out_lat)
        np.save(OUT / f"aod-{when:%Y%m%dT%H%M}.npy", aod)
        print(when, "regional/output min max p99", float(aod.min()), float(aod.max()), float(np.quantile(aod, 0.99)))

        ims: list[np.ndarray] = []
        labs: list[str] = []
        if source_index is not None:
            official = read_source_frame(source_index)[CROP_Y : CROP_Y + CROP_H, CROP_X : CROP_X + CROP_W].copy()
            ims.append(official)
            labs.append(f"Official NASA frame {source_index}")
            cv2.imwrite(str(OUT / f"official-{when:%Y%m%dT%H%M}.png"), official)
        ims.append(base)
        labs.append("Exact source basemap, no added smoke")
        for tau, max_alpha in variants:
            generated = composite(base, aod, gradient, tau=tau, max_alpha=max_alpha)
            ims.append(generated)
            labs.append(f"GEOS-FP tau={tau:.3f} alpha={max_alpha:.2f}")
            cv2.imwrite(
                str(OUT / f"generated-{when:%Y%m%dT%H%M}-tau{tau:.3f}-a{max_alpha:.2f}.png"),
                generated,
            )
        comparison = montage(ims, labs, columns=3)
        cv2.imwrite(str(OUT / f"montage-{when:%Y%m%dT%H%M}.png"), comparison)

    ds.close()


if __name__ == "__main__":
    main()
