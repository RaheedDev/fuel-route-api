# Fuel Route API

A Django REST API that takes a start and a finish location in the USA and returns:

* the driving route (GeoJSON) plus a link to an HTML map of it,
* the cost-optimal fuel stops along the way for a truck with a **500-mile range** at **10 mpg**,
* the **total money spent on fuel**.

Fuel prices come from the provided `fuel-prices-for-be-assessment.csv` (OPIS truck-stop prices).
Routing uses the free, keyless [OSRM](https://project-osrm.org/) API, with **exactly one routing call per new
route** and **zero** for repeated routes (cached).

```
GET /api/route/?start=New York, NY&finish=Los Angeles, CA
→ 2,794 mi · 6 fuel stops · $700.38 · ~25 ms when cached, ~1.2 s on first call (1 OSRM request)
```

---

## Quick start

Requires Python 3.12+ (developed on 3.13). Takes about a minute.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # requirements-dev.txt adds pytest
python manage.py migrate
python manage.py load_fuel_stations      # imports data/fuel-prices-for-be-assessment.csv (~3 s, no API calls)
python manage.py runserver
```

Then open <http://127.0.0.1:8000/api/route/?start=Chicago,%20IL&finish=Houston,%20TX> or import the Postman
collection (below).

Run the tests: `pip install -r requirements-dev.txt && pytest` (71 tests, ~2 s, no network).

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` / `POST` | `/api/route/` | Plan a trip. Query params (GET) or JSON body (POST). |
| `GET` | `/api/route/map/` | Same params; returns an HTML page with a Leaflet/OpenStreetMap map of the route and stops. |
| `GET` | `/api/health/` | Number of stations loaded / located. |
| `GET` | `/api/` | Index of endpoints with example links. |

### Parameters

| Name | Required | Default | Description |
|---|---|---|---|
| `start`, `finish` | yes | | `"City, ST"` (also `"City, State"`, `"City ST"`), a 5-digit ZIP (`"10001"`), `"lat,lng"`, or a street address. |
| `start_fuel` | no | `1.0` | How full the tank is at departure (0 to 1). |
| `stop_penalty` | no | `5` | Planning "cost" in USD of one extra stop (see [Optimiser](#4-optimiser)). `0` = strictly cheapest plan. |
| `corridor_miles` | no | `10` | Max distance of a station from the route (1 to 50). |
| `geometry` | no | `simplified` | Route line detail in the GeoJSON: `simplified` (≤ 2,000 points), `full`, or `none`. |

### Response (trimmed)

```jsonc
{
  "map_url": "http://127.0.0.1:8000/api/route/map/?start=Chicago%2C+IL&finish=Houston%2C+TX",
  "start":  {"query": "Chicago, IL", "label": "Chicago, IL", "lat": 41.85, "lng": -87.6501, "resolved_by": "gazetteer"},
  "finish": {"query": "Houston, TX", "label": "Houston, TX", "lat": 29.7633, "lng": -95.3633, "resolved_by": "gazetteer"},
  "route":  {"distance_miles": 1085.2, "duration_hours": 19.83},
  "fuel_stops": [
    {"stop_number": 1, "opis_id": 69861, "name": "HUCKS FOOD & FUEL #379", "address": "I-57, EXIT 53",
     "city": "Marion", "state": "IL", "lat": 37.7344, "lng": -88.9412, "price_per_gallon": 2.929,
     "mile_marker": 315.9, "distance_from_route_miles": 1.0, "fuel_on_arrival_gallons": 18.4,
     "gallons_purchased": 29.5, "cost": 86.41},
    {"stop_number": 2, "opis_id": 72594, "name": "Quiktrip #7900", "address": "I-30 & FM-989",
     "city": "Texarkana", "state": "TX", "price_per_gallon": 2.857, "mile_marker": 794.0,
     "fuel_on_arrival_gallons": 0.0, "gallons_purchased": 29.1, "cost": 83.15 /* ... */}
  ],
  "summary": {"total_fuel_cost": 169.55, "total_gallons_purchased": 58.6, "number_of_stops": 2,
              "average_price_paid": 2.893, "fuel_burned_gallons": 108.52, "stations_considered": 187},
  "assumptions": {"vehicle_range_miles": 500, "miles_per_gallon": 10, "tank_capacity_gallons": 50.0,
                  "start_fuel_fraction": 1.0, "corridor_miles": 10.0, "stop_penalty_usd": 5.0, "notes": ["..."]},
  "geojson": {"type": "FeatureCollection", "features": ["route LineString", "start/finish/stop Points"]},
  "meta": {
    "route_cache_hit": true,
    "external_api_calls": {"routing": 0, "geocoding": 0, "total": 0},
    "timing_ms": {"geocoding": 0.2, "routing": 1.8, "station_search": 4.8, "optimizer": 1.9,
                  "response_build": 2.2, "routing_api_wait": 0.0, "total": 10.9}
  }
}
```

The `geojson` field can be pasted straight into <https://geojson.io>.

### Errors

All errors look like `{"error": {"code": "...", "message": "..."}}`.

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request` | Missing/invalid parameter (details in `error.fields`). |
| 400 | `location_not_found` | The location can't be resolved. |
| 400 | `outside_usa` | Coordinates outside the US border. |
| 400 | `same_location` | Start and finish are the same place. |
| 422 | `no_route` | OSRM finds no drivable route (e.g. to Hawaii). |
| 422 | `no_fuel_coverage` | Some stretch is longer than 500 mi without a station from the price list. |
| 502 | `upstream_unavailable` | The routing/geocoding service is down or timed out. |

### Postman

Import `postman/FuelRouteAPI.postman_collection.json`. It has a `baseUrl` variable (`http://127.0.0.1:8000`),
the demo routes, a POST example, the HTML map and error cases. Every plan request has a test script that
draws the route on a map in Postman's **Visualize** tab and logs timings to the console.

---

## How it works

```
start, finish ──► 1. geocode (offline) ──► 2. route (1 OSRM call, cached)
              ──► 3. stations near the route (KD-tree) ──► 4. optimiser (DP) ──► JSON + map
```

| File | Role |
|---|---|
| `planner/views.py` | DRF views: validate input, call the planner, map errors to HTTP codes. |
| `planner/serializers.py` | Input validation. |
| `planner/services/planner.py` | Runs the pipeline and builds the response, including timings and the API call count. |
| `planner/services/geocoding.py` | `start` / `finish` → coordinates. |
| `planner/services/gazetteer.py` | Offline US place / ZIP lookup and the "is this in the USA?" check. |
| `planner/services/routing.py` | The single OSRM call, polyline decoding and caching. |
| `planner/services/stations.py` | In-memory station index and the corridor search. |
| `planner/services/optimizer.py` | Pure fuel-stop optimiser (no Django), unit-tested on its own. |
| `planner/services/geometry.py` | Vectorised haversine, densify, polyline decoder. |
| `planner/management/commands/load_fuel_stations.py` | CSV import with offline geocoding. |
| `planner/templates/planner/map.html` | Leaflet map page. |
| `scripts/build_gazetteer.py` | One-off script that built the bundled `data/*.gz` files from Census/USGS sources. |
| `scripts/benchmark.py` | Latency benchmark against a running server. |

### 1. Geocoding without API calls

The price file has no coordinates and there are about 6,600 US stations. Geocoding them online would mean thousands
of calls, so the repo bundles a small **offline gazetteer** (2.3 MB gzipped) built from public-domain data:

* US Census Gazetteer *places* (cities, towns, CDPs) and *county subdivisions* (New England/Midwest towns),
* USGS GNIS *populated places*, used for small unincorporated communities that truck stops are often listed
  under (e.g. "Ruther Glen, VA"). GNIS is also used for town-centre coordinates: Census "internal points" can be
  misleading (San Francisco's is in the Pacific),
* Census ZCTA centroids for ZIP codes, and the Census 1:5M US outline for the "outside the USA" check.

Name matching handles the usual spelling variants: `St./Saint`, `Ft/Fort`, `Mc Calla/McCalla`,
`De Forest/DeForest`, accents (`Cañon City`), `Town of Pecos`, etc.

For requests, `"City, ST"`, ZIP and `"lat,lng"` are resolved offline in under 1 ms. Only free-form addresses that
the gazetteer can't resolve fall back to **one** OpenStreetMap Nominatim request, which is cached permanently and
counted in `meta.external_api_calls.geocoding`. Set `NOMINATIM_URL=""` to run fully offline.

### 2. Routing: one call

`GET {OSRM}/route/v1/driving/{lng,lat};{lng,lat}?overview=full&geometries=polyline` returns distance, duration and
the full route geometry in **one request**. The encoded polyline is about 3x smaller than GeoJSON, which makes the
cold call faster. The decoded route is stored in Django's cache (file-based, 7 days), keyed by the rounded
coordinates, so repeated routes, including after a server restart, make **zero** external calls.
A `requests.Session` keeps the TLS connection to OSRM alive between calls.

### 3. Stations near the route

* All located stations are loaded **once per process** into numpy arrays (reloaded automatically when
  `load_fuel_stations` runs again, through a version key in the cache).
* For each request:
  1. A bounding-box filter keeps only stations near the route's extent.
  2. The route (about 35k OSRM vertices coast to coast) is resampled to one vertex every ≤ 0.5 mi and put in a
     **KD-tree** (scipy `cKDTree`, on 3-D unit-sphere coordinates so distances are correct at any latitude).
  3. One vectorised nearest-neighbour query gives each candidate station its distance from the route and the
     **mile marker** where the route passes it.
* Stations within `corridor_miles` (default 10) are kept. If no feasible plan exists, the corridor is widened to
  25 and then 50 miles before giving up.

### 4. Optimiser

Given stations as `(mile, price)`, a 500-mile tank and 10 mpg, find where to stop and how much to buy.

The strictly cheapest plan is the classic greedy *gas-station problem*: "if a cheaper station is within reach, buy
just enough to get there; otherwise fill up". On real data, though, it makes stops like *"buy 1.2 gallons in
Lexington, NE because the next stations are 3.5¢ more expensive"*: 16 stops coast to coast. So the planner uses a
small **dynamic programme** that minimises

    fuel cost + stop_penalty × number_of_stops        (stop_penalty defaults to $5)

* Fuel is tracked in whole miles of range (0 to 500 units, 1 unit = 0.1 gal).
* Walking the stations in order, `best[f]` = cheapest spend to be here with `f` miles of fuel.
  At each station, every level either drives past or buys more. "Buy" is a prefix-minimum, so each station is one
  vectorised numpy step. Driving to the next station shifts the array down by the distance.
* At the destination, the best final state is traced back to list the stops.

With `stop_penalty=0` it gives the same result as the greedy optimum: the test suite checks this on 25 random
instances. The default $5 halves the number of stops (or better) for well under 1% extra cost:

| Route | Strictly cheapest (`stop_penalty=0`) | Default (`stop_penalty=5`) | Extra cost |
|---|---|---|---:|
| New York → Los Angeles | 16 stops, $693.25 | **6 stops, $700.38** | +$7.13 |
| Seattle → Miami | 18 stops, $848.38 | **8 stops, $851.83** | +$3.45 |
| Denver → Atlanta | 7 stops, $262.05 | **3 stops, $262.57** | +$0.52 |
| Chicago → Houston | 5 stops, $168.71 | **2 stops, $169.55** | +$0.84 |

It takes about 2 to 6 ms for about 450 candidate stations. The penalty is only used to choose stops; it is never
added to `total_fuel_cost`.

### Assumptions

* **The trip starts with a full tank** (`start_fuel=1`), so trips under 500 mi need no stop and cost $0.
  `total_fuel_cost` is the money spent at the pump during the trip. Use `start_fuel` (e.g. `0.25`) to start
  emptier.
* The truck buys only what it needs to reach the destination (arrives near empty).
* 10 mpg and a 500-mile range give a 50-gallon tank. The truck never plans to run dry.
* **Duplicate station IDs** in the CSV (678 IDs, up to 6 rows each, prices up to $0.90 apart) are merged into one
  station at the **lowest** listed price.
* **Non-US rows** (Canadian provinces, 620 rows) are skipped. 6,626 unique US stations remain; **6,595 (99.5%)**
  get coordinates. The 31 whose town isn't in the gazetteer are kept in the DB but not used for routing.
* A station's position is its **town's centre**. The file has no coordinates, so a station can be a few miles off
  its exit, which is why `distance_from_route_miles` is reported. The detour to an off-route station is not added
  to the fuel used.
* Coverage is limited by the price file. For example, it has only 8 California stations, all in the far south, so
  `San Francisco → Dallas` returns `422 no_fuel_coverage` (first station more than 500 mi away). `Los Angeles → Dallas`
  works.

---

## External API calls per request

| Request | Routing (OSRM) | Geocoding | Total |
|---|---|---|---|
| New route, `City, ST` / ZIP / `lat,lng` inputs | **1** | 0 | **1** |
| Same route again (any time within 7 days, even after restart) | 0 | 0 | **0** |
| New route with a free-text street address | 1 | 1 per new address (then cached) | 2 or 3 |
| `/api/route/map/` after the JSON call | 0 | 0 | 0 |
| `load_fuel_stations` (6,600 stations) | 0 | 0 | **0** |

Every response reports this in `meta.external_api_calls`.

---

## Performance

Measured with `scripts/benchmark.py` against `runserver` on the dev box (8-core Xeon VM, Python 3.13,
DEBUG off). **Cold** = route not cached (includes the one OSRM call). **Warm** = cached route.
**Compute** = server time excluding the wait for OSRM. Full details in [PERFORMANCE.md](PERFORMANCE.md).

| Route | Miles | Stops | Fuel cost | Cold total | of which OSRM | Cold compute | Warm total (median) | Warm compute |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| New York, NY → Los Angeles, CA | 2,794 | 6 | $700.38 | 1,226 ms | 1,145 ms | 74 ms | 28 ms | 23 ms |
| Chicago, IL → Houston, TX | 1,085 | 2 | $169.55 | 288 ms | 255 ms | 28 ms | 18 ms | 13 ms |
| Seattle, WA → Miami, FL | 3,302 | 8 | $851.83 | 515 ms | 434 ms | 76 ms | 33 ms | 28 ms |
| Denver, CO → Atlanta, GA | 1,402 | 3 | $262.57 | 301 ms | 258 ms | 38 ms | 20 ms | 14 ms |
| Boston, MA → Philadelphia, PA | 307 | 0 | $0.00 | 270 ms | 246 ms | 19 ms | 11 ms | 6 ms |
| Los Angeles, CA → Dallas, TX | 1,438 | 3 | $260.74 | 306 ms | 260 ms | 41 ms | 16 ms | 12 ms |
| 10001 → 60601 (ZIPs) | 793 | 1 | $89.93 | 297 ms | 256 ms | 36 ms | 17 ms | 12 ms |
| 39.0997,-94.5786 → Salt Lake City, UT | 1,074 | 2 | $170.18 | 282 ms | 250 ms | 28 ms | 15 ms | 11 ms |

Most of the cold latency is the public OSRM server. Our own compute is under 80 ms cold and under 35 ms warm
on every route.

---

## Configuration

Environment variables (all optional, see `.env.example`): `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`,
`DJANGO_ALLOWED_HOSTS`, `OSRM_BASE_URL` (point at a self-hosted OSRM for production; the public demo server has a
fair-use policy), `NOMINATIM_URL` (empty = offline only), `CORRIDOR_MILES`, `STOP_PENALTY_USD`, `CACHE_DIR`.
Vehicle range and MPG are in `config/settings.py` (`VEHICLE_RANGE_MILES`, `VEHICLE_MPG`).

## Limitations and next steps

* Station coordinates are town-level. Geocoding the `Address` field ("I-44, EXIT 283") against interstate-exit data
  would place stations exactly.
* The public OSRM demo server is best-effort. For production, self-host OSRM or switch the provider in
  `routing.py` (e.g. OpenRouteService with an API key).
* Prices are a static snapshot loaded by a management command. A scheduled re-import would keep them current; the
  in-memory index reloads automatically.

## Data sources

* Fuel prices: `data/fuel-prices-for-be-assessment.csv` (provided with the assignment).
* US Census Bureau 2025 Gazetteer files and 2024 cartographic boundary (public domain).
* USGS GNIS Domestic Names (public domain).
* Routing: OSRM demo server (OpenStreetMap data, ODbL). Map tiles and Nominatim: © OpenStreetMap contributors.
