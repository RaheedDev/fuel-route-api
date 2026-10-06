# Performance

## Setup

* Machine: the dev box (8-core Intel Xeon VM, 15 GB RAM), Python 3.13.5, Django 6.1.2, SQLite.
* Server: `DJANGO_DEBUG=false python manage.py runserver --noreload` (single process).
* Client: `scripts/benchmark.py` on the same machine (HTTP over localhost).
* Data: the provided CSV (6,595 located US stations).
* Date: 6 Oct 2026, ~23:00 IST. OSRM = public demo server `router.project-osrm.org`.

How each route is measured:

1. Evict that route from the cache, so the next request is truly **cold**.
2. Send 1 cold request (geocode offline, **1 OSRM call**, compute).
3. Send 5 **warm** requests (route served from cache, 0 external calls) and take the median.

"Compute" is the server's own time = `meta.timing_ms.total - meta.timing_ms.routing_api_wait`.

Reproduce with:

```bash
python manage.py runserver &
python scripts/benchmark.py
```

## Results

| Route | Miles | Stops | Fuel cost | Cold (client) | Routing API | Cold compute | Warm (client, median) | Warm compute (median) | Response size | API calls cold/warm |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| New York, NY → Los Angeles, CA | 2794 | 6 | $700.38 | 1226 ms | 1145 ms | 74 ms | 28 ms | 23 ms | 47 KB | 1 / 0 |
| Chicago, IL → Houston, TX | 1085 | 2 | $169.55 | 288 ms | 255 ms | 28 ms | 18 ms | 13 ms | 41 KB | 1 / 0 |
| Seattle, WA → Miami, FL | 3302 | 8 | $851.83 | 515 ms | 434 ms | 76 ms | 33 ms | 28 ms | 48 KB | 1 / 0 |
| Denver, CO → Atlanta, GA | 1402 | 3 | $262.57 | 301 ms | 258 ms | 38 ms | 20 ms | 14 ms | 43 KB | 1 / 0 |
| Boston, MA → Philadelphia, PA | 307 | 0 | $0.00 | 270 ms | 246 ms | 19 ms | 11 ms | 6 ms | 37 KB | 1 / 0 |
| Los Angeles, CA → Dallas, TX | 1438 | 3 | $260.74 | 306 ms | 260 ms | 41 ms | 16 ms | 12 ms | 44 KB | 1 / 0 |
| 10001 → 60601 | 793 | 1 | $89.93 | 297 ms | 256 ms | 36 ms | 17 ms | 12 ms | 41 KB | 1 / 0 |
| 39.0997,-94.5786 → Salt Lake City, UT | 1074 | 2 | $170.18 | 282 ms | 250 ms | 28 ms | 15 ms | 11 ms | 41 KB | 1 / 0 |

Notes:

* **The OSRM wait is most of the cold time.** It varies from run to run: New York → Los Angeles took between
  590 ms and 1,261 ms over 5 runs; shorter routes are consistently about 250 ms.
* Cold compute is higher than warm compute because it includes decoding the 35k-point polyline and writing it
  to the file cache.
* `/api/route/map/` for New York → Los Angeles (cached route, full-resolution geometry, 811 KB HTML)
  takes about 90 ms.
* On startup, `config/wsgi.py` pre-loads the gazetteer (186k places), ZIPs, the US border and the station index
  (~1.1 s, once per process), so the first request doesn't pay that cost.

## Where the time goes (warm, New York → Los Angeles)

From `meta.timing_ms` (typical):

| Phase | ms | What happens |
|---|---:|---|
| geocoding | 0.1 | Two dict lookups in the in-memory gazetteer. |
| routing | 3.5 | Reading and unpickling the cached route (35k points) from the file cache. |
| station_search | 9.5 | Bounding-box filter → resample route (~12k vertices) → build KD-tree → 1 vectorised query for ~6k stations → 460 stations in the corridor. |
| optimizer | 5.5 | DP over 460 stations × 501 fuel levels (numpy) plus traceback. |
| response_build | 6.5 | One DB query for the 6 stop rows, then the GeoJSON (≤ 2,000 points). |
| **total** | **~25** | |

## Optimisations applied

* **1 routing call, then cache.** The parsed route is cached on disk for 7 days, so repeats skip OSRM, even after
  a restart.
* **Polyline instead of GeoJSON from OSRM:** ~3x smaller download (83 KB vs 277 KB for New York → Los Angeles),
  which made the cold call about 0.4 s faster in one curl comparison (1.05 s vs 1.45 s).
* **Keep-alive HTTP session** to OSRM (no new TLS handshake per call).
* **No per-request DB scan:** stations live in numpy arrays in memory; the DB is only hit for the few chosen stops.
* **KD-tree corridor search** instead of a station × route-point distance matrix. Thinning the 35k OSRM vertices
  before building the tree cut station search from about 19 ms to about 10 ms.
* **Vectorised DP:** each station is O(tank size) numpy work, ~2 to 6 ms in total.
* **Smaller responses:** the route line in JSON is downsampled to ≤ 2,000 points (`geometry=full` gives
  everything), so the payload is ~40 to 50 KB.
