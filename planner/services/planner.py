"""
Orchestrates one trip plan:

    geocode start/finish  ->  get route (1 OSRM call, cached)
    ->  find stations near the route  ->  optimise fuel stops  ->  build JSON
"""

import time
from dataclasses import dataclass

from django.conf import settings

from ..models import FuelStation
from . import geocoding, routing
from .errors import PlannerError, RoutingError
from .geometry import cumulative_miles, downsample, haversine_miles
from .optimizer import InfeasibleRouteError, Station, plan_fuel_stops
from .stations import find_stations_along_route, get_station_index

# If no feasible plan exists within the requested corridor (e.g. a sparse
# stretch of desert), retry with these wider corridors before giving up.
FALLBACK_CORRIDORS_MILES = (25.0, 50.0)

# Aim for at most this many points in the returned (simplified) route line.
SIMPLIFIED_MAX_POINTS = 2000


@dataclass
class PlanOptions:
    start_fuel: float = 1.0  # fraction of a full tank at departure
    corridor_miles: float | None = None  # default: settings.CORRIDOR_MILES
    stop_penalty: float | None = None  # default: settings.STOP_PENALTY_USD
    geometry: str = "simplified"  # simplified | full | none


class Timer:
    """Collects elapsed milliseconds per named phase."""

    def __init__(self):
        self.ms = {}
        self._start = self._last = time.perf_counter()

    def lap(self, name):
        """Add the time since the previous lap to phase ``name``."""
        now = time.perf_counter()
        self.ms[name] = round(self.ms.get(name, 0) + (now - self._last) * 1000, 1)
        self._last = now

    def total(self):
        return round((time.perf_counter() - self._start) * 1000, 1)


def plan_trip(start_query, finish_query, options=None):
    options = options or PlanOptions()
    timer = Timer()
    range_miles, mpg = settings.VEHICLE_RANGE_MILES, settings.VEHICLE_MPG

    # 1) Geocode (offline for 'City, ST', ZIP and 'lat,lng').
    start = geocoding.geocode(start_query)
    finish = geocoding.geocode(finish_query)
    if haversine_miles(start.lat, start.lng, finish.lat, finish.lng) < 1:
        raise PlannerError("Start and finish are the same place.", code="same_location")
    timer.lap("geocoding")

    # 2) Route: exactly one call to the routing API, or zero if cached.
    route = routing.get_route(start.point, finish.point)
    timer.lap("routing")

    # 3 + 4) Stations along the route, then the cost-optimal refuelling plan.
    nearby, plan, corridor_used = _optimise(route, options, timer)

    # 5) Response.
    stops = _describe_stops(plan, nearby)
    total_cost, total_gallons = plan.total_cost, plan.total_gallons
    response = {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "route": {
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2),
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(total_cost, 2),
            "total_gallons_purchased": round(total_gallons, 2),
            "number_of_stops": len(stops),
            "average_price_paid": round(total_cost / total_gallons, 3) if total_gallons else None,
            "fuel_burned_gallons": round(route.distance_miles / mpg, 2),
            "stations_considered": len(nearby),
        },
        "assumptions": {
            "vehicle_range_miles": range_miles,
            "miles_per_gallon": mpg,
            "tank_capacity_gallons": range_miles / mpg,
            "start_fuel_fraction": options.start_fuel,
            "corridor_miles": corridor_used,
            "stop_penalty_usd": _stop_penalty(options),
            "notes": [
                "The trip starts with the tank at start_fuel_fraction (default: full); "
                "total_fuel_cost is the money spent at fuel stops during the trip.",
                "Only enough fuel to reach the destination is bought (arrive near empty).",
                "Plans minimise fuel cost + stop_penalty_usd per stop, so the optimiser "
                "won't stop just to save a few cents; the penalty is not part of total_fuel_cost.",
                "Station positions are their town's centroid (the price file has no coordinates).",
            ],
        },
        "geojson": _geojson(route, start, finish, stops, options.geometry),
    }
    timer.lap("response_build")

    calls = {
        "routing": 0 if route.from_cache else 1,
        "geocoding": int(start.api_call) + int(finish.api_call),
    }
    calls["total"] = sum(calls.values())
    response["meta"] = {
        "route_cache_hit": route.from_cache,
        "external_api_calls": calls,
        "timing_ms": {**timer.ms, "routing_api_wait": round(route.api_ms, 1), "total": timer.total()},
    }
    return response


def _describe_stops(plan, nearby):
    """Optimiser output + station details from the DB (one query) -> JSON-ready dicts."""
    station_id = {stop.station.ref: int(nearby.station_ids[stop.station.ref]) for stop in plan.stops}
    details = FuelStation.objects.in_bulk(station_id.values())
    stops = []
    for number, stop in enumerate(plan.stops, start=1):
        k = stop.station.ref  # row in ``nearby``
        st = details[station_id[k]]
        stops.append({
            "stop_number": number,
            "opis_id": st.opis_id,
            "name": st.name,
            "address": st.address,
            "city": st.city,
            "state": st.state,
            "lat": round(float(nearby.lat[k]), 5),
            "lng": round(float(nearby.lng[k]), 5),
            "price_per_gallon": round(float(st.price), 3),
            "mile_marker": round(stop.station.mile, 1),
            "distance_from_route_miles": round(float(nearby.offset_miles[k]), 1),
            "fuel_on_arrival_gallons": round(stop.fuel_on_arrival_gallons, 2),
            "gallons_purchased": round(stop.gallons, 2),
            "cost": round(stop.cost, 2),
        })
    return stops


def _stop_penalty(options):
    return settings.STOP_PENALTY_USD if options.stop_penalty is None else options.stop_penalty


def _optimise(route, options, timer):
    """Find nearby stations and plan stops, widening the corridor if needed."""
    index = get_station_index()
    first = options.corridor_miles or settings.CORRIDOR_MILES
    corridors = [first] + [c for c in FALLBACK_CORRIDORS_MILES if c > first]
    for corridor in corridors:
        nearby = find_stations_along_route(index, route.coords, corridor, route.distance_miles)
        timer.lap("station_search")
        # ``ref`` lets us map optimiser output back to rows of ``nearby``.
        candidates = [
            Station(mile=float(m), price=float(p), ref=i)
            for i, (m, p) in enumerate(zip(nearby.mile, nearby.price))
        ]
        try:
            plan = plan_fuel_stops(
                candidates,
                route.distance_miles,
                tank_range_miles=settings.VEHICLE_RANGE_MILES,
                mpg=settings.VEHICLE_MPG,
                start_fuel_fraction=options.start_fuel,
                stop_penalty=_stop_penalty(options),
            )
            return nearby, plan, corridor
        except InfeasibleRouteError as exc:
            error = exc
        finally:
            timer.lap("optimizer")
    raise RoutingError(
        f"Cannot complete this trip on a {settings.VEHICLE_RANGE_MILES}-mile tank using the "
        f"stations in the price list (searched up to {corridors[-1]:.0f} mi from the route): {error}",
        code="no_fuel_coverage",
    ) from error


def _geojson(route, start, finish, stops, mode):
    """A FeatureCollection with the route line and all points (paste into geojson.io)."""
    features = []
    if mode != "none":
        coords = route.coords
        if mode == "simplified":
            miles = cumulative_miles(coords)
            coords = downsample(coords, miles, max(0.1, miles[-1] / SIMPLIFIED_MAX_POINTS))
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(lng, 5), round(lat, 5)] for lat, lng in coords.tolist()],
            },
            "properties": {"kind": "route", "distance_miles": round(route.distance_miles, 1)},
        })
    for kind, loc in (("start", start), ("finish", finish)):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(loc.lng, 5), round(loc.lat, 5)]},
            "properties": {"kind": kind, "label": loc.label},
        })
    for stop in stops:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [stop["lng"], stop["lat"]]},
            "properties": {"kind": "fuel_stop", **stop},
        })
    return {"type": "FeatureCollection", "features": features}
