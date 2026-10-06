"""
Turn the user's ``start`` / ``finish`` strings into coordinates.

Accepted inputs, tried in this order (first three need NO network call):

1. ``"40.7128,-74.0060"``            - latitude,longitude
2. ``"10001"``                       - a 5-digit US ZIP code
3. ``"Chicago, IL"`` / ``"Chicago, Illinois"`` / ``"Chicago IL"`` - city + state,
   looked up in the bundled offline gazetteer
4. anything else (e.g. a street address) - one call to OpenStreetMap
   Nominatim, cached forever, so a repeated address costs nothing.
"""

import hashlib
import logging
import re
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from . import gazetteer
from .errors import GeocodingError, UpstreamError

logger = logging.getLogger(__name__)

LATLNG_RE = re.compile(r"^\s*([-+]?\d{1,3}(?:\.\d+)?)\s*,\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")
ZIP_RE = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")
TRAILING_ZIP_RE = re.compile(r"\s+\d{5}(?:-\d{4})?$")

_session = requests.Session()


@dataclass
class Location:
    query: str
    label: str
    lat: float
    lng: float
    source: str  # coordinates | zip | gazetteer | nominatim
    api_call: bool = False  # True if an external request was made

    @property
    def point(self):
        return (self.lat, self.lng)

    def as_dict(self):
        return {
            "query": self.query,
            "label": self.label,
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            "resolved_by": self.source,
        }


def geocode(query):
    """Resolve a location string, raising ``GeocodingError`` if impossible."""
    query = (query or "").strip()
    if not query:
        raise GeocodingError("Location is empty.")

    location = (
        _from_coordinates(query)
        or _from_zip(query)
        or _from_city_state(query)
        or _from_nominatim(query)
    )
    if location is None:
        raise GeocodingError(
            f"Could not find '{query}' in the USA. Use 'City, ST' (e.g. 'Dallas, TX'), "
            "a 5-digit ZIP code, or 'lat,lng'."
        )
    # Gazetteer/ZIP/Nominatim(countrycodes=us) results are US by construction;
    # raw coordinates must be checked against the US outline.
    if location.source == "coordinates" and not gazetteer.is_in_usa(location.lat, location.lng):
        raise GeocodingError(
            f"'{query}' resolves to ({location.lat:.4f}, {location.lng:.4f}), which is "
            "outside the USA.",
            code="outside_usa",
        )
    return location


def _from_coordinates(query):
    match = LATLNG_RE.match(query)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise GeocodingError(f"'{query}' is not a valid 'lat,lng' pair.", code="invalid_coordinates")
    return Location(query, f"{lat:.5f},{lng:.5f}", lat, lng, "coordinates")


def _from_zip(query):
    match = ZIP_RE.match(query)
    if not match:
        return None
    point = gazetteer.find_zip(match.group(1))
    if point is None:
        raise GeocodingError(f"Unknown US ZIP code '{query}'.")
    return Location(query, f"ZIP {match.group(1)}", *point, "zip")


def _split_city_state(query):
    """Yield plausible (city, state) splits of the query."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) >= 2:
        # "..., City, ST 12345" -> city = second-to-last part, state = last part
        yield parts[-2], TRAILING_ZIP_RE.sub("", parts[-1])
    words = parts[-1].split() if parts else []
    # "Chicago IL", "Salt Lake City Utah", "Kansas City New Mexico"
    for n in (1, 2):
        if len(words) > n:
            yield " ".join(words[:-n]), " ".join(words[-n:])


def _from_city_state(query):
    for city, state in _split_city_state(query):
        place = gazetteer.find_place(city, state)
        if place:
            return Location(query, f"{place.name}, {place.state}", place.lat, place.lng, "gazetteer")
    return None


def _from_nominatim(query):
    """Last resort: one cached request to OpenStreetMap's free geocoder."""
    if not settings.NOMINATIM_URL:
        return None
    normalized = re.sub(r"\s+", " ", query.lower())
    key = "geocode:v1:" + hashlib.sha1(normalized.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return Location(**cached) if cached else None

    started = time.perf_counter()
    try:
        response = _session.get(
            settings.NOMINATIM_URL.rstrip("/") + "/search",
            params={"q": query, "format": "jsonv2", "countrycodes": "us", "limit": 1},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise UpstreamError("The geocoding service is unavailable, please retry.") from exc
    logger.info("Nominatim call %.0f ms for %r", (time.perf_counter() - started) * 1000, query)

    if not results:
        cache.set(key, {})  # remember misses too
        return None
    hit = results[0]
    data = {
        "query": query,
        "label": hit.get("display_name", query),
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "source": "nominatim",
    }
    cache.set(key, data, timeout=None)
    return Location(**data, api_call=True)
