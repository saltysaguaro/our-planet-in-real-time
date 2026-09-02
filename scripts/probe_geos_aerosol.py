#!/usr/bin/env python3
"""Probe NASA GMAO's public GEOS-FP aerosol OPeNDAP archive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from pydap.client import open_url

URL = "https://opendap.nccs.nasa.gov/dods/GEOS-5/fp/0.25_deg/seamless/tavg3_2d_aer_Nx.latest"


def decode_grads_time(values: np.ndarray) -> list[datetime]:
    # GDS exposes DAP time as ordinal-like days since 0001-01-01 00:00.
    # Python's fromordinal(1) is 0001-01-01, so subtract one day.
    epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
    return [epoch + timedelta(days=float(v) - 1.0) for v in values]


def main() -> None:
    report: list[str] = []
    report.append(f"URL={URL}")
    ds = open_url(URL, protocol="dap2")
    report.append("variables=" + ",".join(sorted(ds.keys())))

    t_raw = np.asarray(ds["time"][:].data, dtype=float)
    times = decode_grads_time(t_raw)
    report.append(f"time_count={len(times)}")
    report.append(f"time_start={times[0].isoformat()}")
    report.append(f"time_end={times[-1].isoformat()}")

    lat = np.asarray(ds["lat"][:].data, dtype=float)
    lon = np.asarray(ds["lon"][:].data, dtype=float)
    report.append(f"lat_count={lat.size}; lat_range={lat[0]}..{lat[-1]}")
    report.append(f"lon_count={lon.size}; lon_range={lon[0]}..{lon[-1]}")
    report.append(f"brexttau_shape={ds['brexttau'].shape}")

    lat_indices = np.flatnonzero((lat >= 20.0) & (lat <= 50.0))
    lon_indices = np.flatnonzero((lon >= -20.0) & (lon <= 40.0))
    yi0, yi1 = int(lat_indices[0]), int(lat_indices[-1]) + 1
    xi0, xi1 = int(lon_indices[0]), int(lon_indices[-1]) + 1

    # Pull a small final-time regional sample to verify server-side subsetting.
    sample = np.asarray(ds["brexttau"][-1, yi0:yi1:8, xi0:xi1:8].data, dtype=float)
    finite = sample[np.isfinite(sample) & (sample < 1e10)]
    report.append(f"sample_shape={sample.shape}")
    report.append(f"sample_min={float(finite.min()) if finite.size else 'none'}")
    report.append(f"sample_max={float(finite.max()) if finite.size else 'none'}")

    out = Path("build/geos-probe.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
