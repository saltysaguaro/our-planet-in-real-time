# Our Planet in Real-Time

A GitHub Pages repository for a growing collection of live climate graphics.

The first published page is:

- `Southwest U.S. / Daily Maximum Temperature`

## Repository shape

- `index.html`: landing page for the collection
- `config/site.json`: catalog of live pages
- `series/<slug>/`: one folder per live page
- `data/series/<slug>.json`: one generated dataset per page
- `assets/`: shared styling and shared browser code
- `scripts/update_data.py`: rebuilds all configured datasets

This keeps the first page simple to launch now while leaving room to add more pages later without reorganizing the repo again.

## Data freshness

The site checks NOAA every hour and republishes automatically when new data appears. It is only as current as NOAA's public `nClimGrid-Daily` release, so this is near-live daily data rather than literal real-time sensor data.

## Run locally

```bash
python3 scripts/update_data.py
python3 -m http.server 8000
```

Then open `http://localhost:8000`.

## Adding the next live page

1. Add a new entry to `config/site.json`.
2. Add a new `series/<slug>/index.html` page that uses the shared assets.
3. Extend `scripts/update_data.py` if the new page needs a new data source or chart type.

## Publish

Push the repo to GitHub, enable GitHub Pages from the `main` branch root, and leave GitHub Actions enabled. The workflow will keep every configured dataset in `data/series/` refreshed automatically.
