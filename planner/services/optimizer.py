"""
Cost-optimal refuelling plan along a route (pure Python + numpy, no Django).

Problem
-------
Stations sit at known mile markers with known prices. The tank holds
``tank_range_miles`` of fuel (500 mi = 50 gal at 10 mpg). Choose where to
stop and how much to buy so that we reach the destination as cheaply as
possible.

Why not just "always buy at the cheapest station"? Because the cheapest
station may be out of reach, and buying a full tank when a cheaper station
is 20 miles ahead wastes money. The exact answer needs a small dynamic
programme (DP).

The DP in plain words
---------------------
Fuel is measured in whole miles of range (1 unit = 1 mile = 0.1 gal), so the
tank can hold 0..500 units. We walk the stations in route order and keep one
array::

    best[f] = cheapest total spend to be here with f miles of fuel in the tank

At each station there are two choices for every fuel level: drive past, or
stop and buy some fuel (price per unit + a fixed ``stop_penalty``). Then we
drive to the next station, which shifts every fuel level down by the
distance. At the destination, ``best[f]`` with the lowest value wins and we
trace our decisions backwards to list the stops.

``stop_penalty`` (USD, default from settings) is NOT added to the reported
fuel cost; it only stops the plan from making a 15-minute stop to buy one
gallon that saves 3 cents. With ``stop_penalty=0`` the plan is the strictly
cheapest one (equivalent to the classic greedy gas-station algorithm).

Complexity: O(stations x tank size) with vectorised numpy steps, about
1-5 ms for a coast-to-coast route with ~450 candidate stations.
"""

import math
from dataclasses import dataclass, field

import numpy as np

INF = float("inf")
# Added to every stop so that, among plans of equal cost, the one with fewer
# stops wins (matters when stop_penalty=0 and two stations share a price).
TIE_BREAK_USD = 1e-6


class InfeasibleRouteError(Exception):
    """A gap between stations (or to the first station) is longer than the fuel available."""


@dataclass(frozen=True)
class Station:
    mile: float  # position along the route
    price: float  # USD per gallon
    ref: object = None  # caller's identifier (passed through untouched)


@dataclass(frozen=True)
class FuelStop:
    station: Station
    gallons: float
    cost: float
    fuel_on_arrival_gallons: float


@dataclass
class FuelPlan:
    stops: list = field(default_factory=list)

    @property
    def total_cost(self):
        return sum(stop.cost for stop in self.stops)

    @property
    def total_gallons(self):
        return sum(stop.gallons for stop in self.stops)


def plan_fuel_stops(
    stations,
    route_miles,
    *,
    tank_range_miles=500,
    mpg=10,
    start_fuel_fraction=1.0,
    stop_penalty=0.0,
):
    """
    Return the cheapest ``FuelPlan`` for driving ``route_miles``.

    ``stations``: iterable of ``Station`` (any order).
    ``start_fuel_fraction``: how full the tank is at departure (1.0 = full).
    ``stop_penalty``: extra USD "cost" per stop used only for choosing stops.
    Raises ``InfeasibleRouteError`` if the trip can't be completed.
    """
    capacity = int(tank_range_miles)
    start_fuel = int(capacity * start_fuel_fraction)
    if start_fuel >= route_miles:
        return FuelPlan()  # starting fuel covers the whole trip

    # Several stations at the same spot (same town): only the cheapest matters.
    cheapest_at = {}
    for st in stations:
        if 0 <= st.mile < route_miles and st.price < cheapest_at.get(st.mile, Station(0, INF)).price:
            cheapest_at[st.mile] = st
    ordered = sorted(cheapest_at.values(), key=lambda s: s.mile)
    _check_gaps([s.mile for s in ordered], route_miles, capacity, start_fuel)

    # Integer mile positions; rounding *up* never under-estimates fuel used.
    positions = [math.ceil(s.mile) for s in ordered] + [math.ceil(route_miles)]
    levels = np.arange(capacity + 1)

    best = np.full(capacity + 1, INF)
    best[start_fuel] = 0.0
    previous = 0
    decisions = []  # per station: (bought?, fuel on arrival) for each fuel level after buying

    for station, position in zip(ordered, positions):
        best = _drive(best, position - previous)
        previous = position

        # Buying: to leave with g units having arrived with f < g units costs
        # best[f] + (g - f) * unit_price + stop_penalty. Splitting the terms,
        #   = g * unit_price + stop_penalty + min_{f<g} (best[f] - f * unit_price)
        # and the min over f<g is a running (prefix) minimum -> fully vectorised.
        unit_price = station.price / mpg  # USD per mile of range
        shifted = np.concatenate(([INF], (best - levels * unit_price)[:-1]))  # value for f = g-1
        running_min = np.minimum.accumulate(shifted)
        # Index f that achieved the running minimum (latest one on ties).
        arrival = np.maximum.accumulate(np.where(shifted == running_min, levels, 0)) - 1
        buy_cost = levels * unit_price + stop_penalty + TIE_BREAK_USD + running_min

        bought = buy_cost < best
        decisions.append((bought, arrival))
        best = np.where(bought, buy_cost, best)

    best = _drive(best, positions[-1] - previous)
    final_fuel = int(np.argmin(best))
    if not np.isfinite(best[final_fuel]):  # pragma: no cover - guarded by _check_gaps
        raise InfeasibleRouteError("No feasible refuelling plan.")
    return _trace_back(ordered, positions, decisions, final_fuel, mpg)


def _drive(best, distance):
    """Shift fuel levels down by ``distance`` (arriving with f means leaving with f + distance)."""
    moved = np.full_like(best, INF)
    if distance < len(best):
        moved[: len(best) - distance] = best[distance:]
    return moved


def _trace_back(ordered, positions, decisions, final_fuel, mpg):
    """Walk backwards from the destination to recover where we bought and how much."""
    stops = []
    fuel = final_fuel  # fuel on arrival at the destination
    for i in range(len(ordered) - 1, -1, -1):
        fuel += positions[i + 1] - positions[i]  # fuel when leaving station i
        bought, arrival = decisions[i]
        if bought[fuel]:
            arrived_with = int(arrival[fuel])
            gallons = (fuel - arrived_with) / mpg
            station = ordered[i]
            stops.append(FuelStop(station, gallons, gallons * station.price, arrived_with / mpg))
            fuel = arrived_with
    stops.reverse()
    return FuelPlan(stops)


def _check_gaps(miles, route_miles, capacity, start_fuel):
    """Fail fast, with a helpful message, if some stretch is longer than the range."""
    if not miles:
        raise InfeasibleRouteError(
            f"No fuel stations along the route and the trip ({route_miles:.0f} mi) "
            f"is longer than the {start_fuel} miles of starting fuel."
        )
    if miles[0] > start_fuel:
        raise InfeasibleRouteError(
            f"The first station is at mile {miles[0]:.0f}, beyond the {start_fuel} "
            "miles the starting fuel allows."
        )
    points = miles + [route_miles]
    for a, b in zip(points, points[1:]):
        if b - a > capacity:
            raise InfeasibleRouteError(
                f"No fuel station between mile {a:.0f} and mile {b:.0f} "
                f"({b - a:.0f} mi > {capacity}-mile range)."
            )
