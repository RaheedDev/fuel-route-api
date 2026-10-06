"""
In-memory spatial index of fuel stations + "which stations are on my route?".

All ~6,700 located stations are loaded from SQLite into numpy arrays once per
process (a few ms) and reused for every request. If ``load_fuel_stations``
is run again, it bumps a version number in the cache and every running
server process reloads on its next request.
"""

import threading
import time
from dataclasses import dataclass

import numpy as np
from django.core.cache import cache
from scipy.spatial import cKDTree

from ..models import FuelStation
from .geometry import (
    chord_to_miles,
    cumulative_miles,
    densify,
    downsample,
    miles_to_chord,
    to_unit_xyz,
)

VERSION_CACHE_KEY = "stations:version"

# Resample the route to a vertex every <= 0.5 mi, so the nearest route
# *vertex* is within ~0.25 mi of the nearest point on the route *line*.
ROUTE_STEP_MILES = 0.5


@dataclass
class StationIndex:
    version: object
    ids: np.ndarray  # FuelStation primary keys
    lat: np.ndarray
    lng: np.ndarray
    price: np.ndarray

    @classmethod
    def load(cls, version=None):
        rows = list(
            FuelStation.objects.filter(latitude__isnull=False, longitude__isnull=False)
            .values_list("id", "latitude", "longitude", "price")
        )
        data = np.array(rows, dtype=float).reshape(-1, 4)
        return cls(version, data[:, 0].astype(int), data[:, 1], data[:, 2], data[:, 3])

    def __len__(self):
        return len(self.ids)


_index = None
_lock = threading.Lock()


def get_station_index():
    """Return the shared StationIndex, reloading it if the data changed."""
    global _index
    version = cache.get(VERSION_CACHE_KEY)
    if _index is None or _index.version != version:
        with _lock:
            if _index is None or _index.version != version:
                _index = StationIndex.load(version)
    return _index


def mark_stations_changed():
    """Called after (re)loading stations so all processes rebuild their index."""
    cache.set(VERSION_CACHE_KEY, time.time(), timeout=None)


@dataclass
class RouteStations:
    """Stations near the route, as parallel arrays sorted by mile marker."""

    station_ids: np.ndarray
    mile: np.ndarray  # distance along the route where the station is reached
    offset_miles: np.ndarray  # straight-line distance from the route
    price: np.ndarray
    lat: np.ndarray
    lng: np.ndarray

    def __len__(self):
        return len(self.station_ids)

    @classmethod
    def empty(cls):
        return cls(*(np.empty(0) for _ in range(6)))


def find_stations_along_route(index, route_coords, corridor_miles, route_distance_miles=None):
    """
    Find stations within ``corridor_miles`` of the route and the mile marker
    at which the route passes each of them.

    1. Bounding-box pre-filter (cheap numpy comparison).
    2. KD-tree over the resampled route; one vectorised nearest-neighbour
       query for all candidate stations gives distance-to-route and the
       index of the closest route point (-> mile marker).

    If ``route_distance_miles`` (the router's official distance) is given,
    mile markers are scaled to it so they add up exactly.
    """
    if len(index) == 0:
        return RouteStations.empty()

    # 1) Bounding box of the route, padded by the corridor width.
    pad_lat = corridor_miles / 69.0
    max_abs_lat = np.abs(route_coords[:, 0]).max()
    pad_lng = corridor_miles / (69.0 * max(np.cos(np.radians(max_abs_lat)), 0.1))
    (lat_min, lng_min), (lat_max, lng_max) = route_coords.min(0), route_coords.max(0)
    in_box = np.flatnonzero(
        (index.lat >= lat_min - pad_lat) & (index.lat <= lat_max + pad_lat)
        & (index.lng >= lng_min - pad_lng) & (index.lng <= lng_max + pad_lng)
    )
    if in_box.size == 0:
        return RouteStations.empty()

    # 2) Nearest point on the (resampled) route for each candidate station.
    # OSRM returns tens of thousands of vertices (very dense in cities):
    # thin them to ~1 per 0.25 mi, then fill long straight gaps.
    thinned = downsample(route_coords, cumulative_miles(route_coords), ROUTE_STEP_MILES / 2)
    dense = densify(thinned, ROUTE_STEP_MILES)
    dense_miles = cumulative_miles(dense)
    tree = cKDTree(to_unit_xyz(dense[:, 0], dense[:, 1]))
    chord, nearest = tree.query(
        to_unit_xyz(index.lat[in_box], index.lng[in_box]),
        distance_upper_bound=float(miles_to_chord(corridor_miles)),
    )
    hit = np.isfinite(chord)  # misses come back as inf
    selected, nearest = in_box[hit], nearest[hit]

    if route_distance_miles and dense_miles[-1] > 0:
        # Our haversine sum differs from the router's distance by <1%;
        # scale so mile markers line up with the official route distance.
        dense_miles = dense_miles * (route_distance_miles / dense_miles[-1])

    order = np.argsort(dense_miles[nearest], kind="stable")
    selected, nearest = selected[order], nearest[order]
    return RouteStations(
        station_ids=index.ids[selected],
        mile=dense_miles[nearest],
        offset_miles=chord_to_miles(chord[hit][order]),
        price=index.price[selected],
        lat=index.lat[selected],
        lng=index.lng[selected],
    )
