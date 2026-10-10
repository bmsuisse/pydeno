# Vendored real-world data

A small, anonymized sample of real NYC taxi trips, for `examples/monty_taxi_flows.py` (Monty
computes zone density and flow statistics over it; pydeno/three.js renders them). Every other
vendored example dataset in this project is synthetic (no network, no external data) — this one
is real, by request, so the trade-off is spelled out here rather than silently accepted.

| File | Rows | Bytes | Source |
|---|---:|---:|---|
| `nyc_taxi_trips_2025-01_sample.csv` | 60,000 | 4,405,756 | NYC TLC Yellow Taxi trip records, January 2025 |
| `nyc_taxi_zones.csv` | 263 | 16,216 | NYC TLC taxi zone lookup + shapefile centroids |

## Provenance

- Trip records: `https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-01.parquet`
  (3,475,226 rows for the month). Filtered to rows with both pickup and dropoff zones known,
  a positive trip distance under 100 miles, a positive fare under $500, and at least one
  passenger (2,792,359 rows matched), then reservoir-sampled (seed `20250101`) down to 60,000
  rows, trimmed to 10 columns (pickup/dropoff time, passenger count, trip distance, pickup/dropoff
  zone ID, payment type, fare, tip, total).
- Zone geometry: `https://d37ci6vzurychx.cloudfront.net/misc/taxi_zones.zip` (a shapefile in
  EPSG:2263, NY State Plane Long Island, US feet), reprojected to WGS84 and reduced to each
  zone's largest-ring bounding-box center — good enough to place a zone on a map, not a precise
  polygon centroid.
- Zone names/boroughs: `https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv`.
- All three are published by the NYC Taxi & Limousine Commission at
  `https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page`. The page states no formal
  license; it is NYC open government data, published for public use, with a stated disclaimer
  that the TLC "makes no representations as to the accuracy" of trip data supplied by its
  technology vendors — don't treat the sample as ground truth, it is realistic example data.
- Trips carry no name, address or payment detail — only a pickup/dropoff zone (not a street
  address), time, distance and amounts, the same granularity TLC itself publishes.

## Reproducing or updating the sample

```bash
curl -O https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_YYYY-MM.parquet
curl -O https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv
curl -O https://d37ci6vzurychx.cloudfront.net/misc/taxi_zones.zip
# then: unzip taxi_zones.zip; reproject EPSG:2263 -> EPSG:4326 (pyproj) for each zone's
# largest-ring bbox center; reservoir-sample the parquet's valid rows down to ~60k; see the
# commit that added this file for the exact script.
```
