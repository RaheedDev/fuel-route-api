"""
Measure real API latency against a running server.

    python manage.py runserver            # in another terminal
    python scripts/benchmark.py           # prints a Markdown table

For every route it first evicts that route from the cache (so the first
request is truly "cold" and calls OSRM once), then makes one cold request
and N warm (cached) requests.
"""

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402

from planner.services import geocoding, routing  # noqa: E402

ROUTES = [
    ("New York, NY", "Los Angeles, CA"),
    ("Chicago, IL", "Houston, TX"),
    ("Seattle, WA", "Miami, FL"),
    ("Denver, CO", "Atlanta, GA"),
    ("Boston, MA", "Philadelphia, PA"),
    ("Los Angeles, CA", "Dallas, TX"),
    ("10001", "60601"),  # ZIP codes: Manhattan -> Chicago Loop
    ("39.0997,-94.5786", "Salt Lake City, UT"),  # raw coordinates (Kansas City)
]


def timed_get(url, params):
    started = time.perf_counter()
    response = requests.get(url, params=params, timeout=60)
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return elapsed, response.json(), len(response.content)


def compute_ms(timing):
    """Server time excluding the wait for the routing API."""
    return timing["total"] - timing["routing_api_wait"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--warm-runs", type=int, default=5)
    args = parser.parse_args()
    url = args.base_url.rstrip("/") + "/api/route/"

    print("| Route | Miles | Stops | Fuel cost | Cold (client) | Routing API | Cold compute | "
          "Warm (client, median) | Warm compute (median) | Response size | API calls cold/warm |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for start, finish in ROUTES:
        a, b = geocoding.geocode(start), geocoding.geocode(finish)
        cache.delete(routing._cache_key(a.point, b.point))
        params = {"start": start, "finish": finish}

        cold_ms, cold, size = timed_get(url, params)
        warm = [timed_get(url, params) for _ in range(args.warm_runs)]
        warm_client = statistics.median(w[0] for w in warm)
        warm_compute = statistics.median(compute_ms(w[1]["meta"]["timing_ms"]) for w in warm)
        t = cold["meta"]["timing_ms"]
        print(
            f"| {start} → {finish} | {cold['route']['distance_miles']:.0f} | "
            f"{cold['summary']['number_of_stops']} | ${cold['summary']['total_fuel_cost']:.2f} | "
            f"{cold_ms:.0f} ms | {t['routing_api_wait']:.0f} ms | {compute_ms(t):.0f} ms | "
            f"{warm_client:.0f} ms | {warm_compute:.0f} ms | {size / 1024:.0f} KB | "
            f"{cold['meta']['external_api_calls']['total']} / "
            f"{warm[-1][1]['meta']['external_api_calls']['total']} |"
        )


if __name__ == "__main__":
    main()
