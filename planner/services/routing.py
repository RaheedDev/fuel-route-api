"""
Driving route between two points via the free, keyless OSRM API.

Exactly ONE HTTP request per (start, finish) pair; the parsed result is kept
in Django's cache (file-based by default), so repeated requests make zero
external calls.
"""

import logging
import time
from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

from .errors import RoutingError, UpstreamError
from .geometry import decode_polyline

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
CACHE_VERSION = "v1"

# Re-use one TCP/TLS connection across requests (saves ~100-200 ms per call).
_session = requests.Session()


@dataclass
class Route:
    coords: np.ndarray  # (N, 2) array of (lat, lng)
    distance_miles: float
    duration_seconds: float
    from_cache: bool = False
    api_ms: float = 0.0  # time spent waiting for the routing API


def _cache_key(start, finish):
    return "route:{}:{:.5f},{:.5f}:{:.5f},{:.5f}".format(CACHE_VERSION, *start, *finish)


def get_route(start, finish):
    """Return the driving route from ``start`` to ``finish`` ((lat, lng) tuples)."""
    key = _cache_key(start, finish)
    cached = cache.get(key)
    if cached is not None:
        return Route(**cached, from_cache=True)

    route_data, api_ms = _fetch_from_osrm(start, finish)
    cache.set(key, route_data)
    return Route(**route_data, api_ms=api_ms)


def _fetch_from_osrm(start, finish):
    # OSRM wants lng,lat order.
    url = "{}/route/v1/driving/{:.6f},{:.6f};{:.6f},{:.6f}".format(
        settings.OSRM_BASE_URL.rstrip("/"), start[1], start[0], finish[1], finish[0]
    )
    params = {"overview": "full", "geometries": "polyline", "steps": "false"}
    started = time.perf_counter()
    try:
        response = _session.get(
            url,
            params=params,
            timeout=settings.ROUTING_TIMEOUT_SECONDS,
            headers={"User-Agent": settings.HTTP_USER_AGENT},
        )
    except requests.RequestException as exc:
        logger.warning("OSRM request failed: %s", exc)
        raise UpstreamError("The routing service is unreachable, please retry.") from exc
    api_ms = (time.perf_counter() - started) * 1000
    logger.info("OSRM call %.0f ms (HTTP %s)", api_ms, response.status_code)

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("code")
    if code in {"NoRoute", "NoSegment"}:
        raise RoutingError("No drivable route was found between these locations.")
    if response.status_code != 200 or code != "Ok" or not payload.get("routes"):
        raise UpstreamError(
            f"The routing service returned an error ({response.status_code} {code or ''}).".strip()
        )

    best = payload["routes"][0]
    data = {
        "coords": decode_polyline(best["geometry"]),
        "distance_miles": best["distance"] / METERS_PER_MILE,
        "duration_seconds": best["duration"],
    }
    return data, api_ms
