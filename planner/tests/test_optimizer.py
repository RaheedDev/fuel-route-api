import random
from bisect import bisect_right

import pytest

from planner.services.optimizer import InfeasibleRouteError, Station, plan_fuel_stops


def plan(stations, route_miles, **kwargs):
    kwargs.setdefault("stop_penalty", 0)
    return plan_fuel_stops([Station(m, p) for m, p in stations], route_miles, **kwargs)


def stops_summary(result):
    return [(s.station.mile, round(s.gallons, 6)) for s in result.stops]


def test_no_stop_needed_when_starting_fuel_covers_trip():
    result = plan([(100, 3.0)], 450)
    assert result.stops == [] and result.total_cost == 0


def test_skips_expensive_station_when_a_cheaper_one_is_in_reach():
    # Full tank (500 mi). Driving past the $4 station to the $3 one is cheaper.
    result = plan([(100, 4.0), (400, 3.0)], 800)
    # Arrive at mile 400 with 100 mi left; 400 mi to go -> buy 300 mi = 30 gal.
    assert stops_summary(result) == [(400, 30.0)]
    assert result.total_cost == pytest.approx(90.0)


def test_fills_up_at_cheap_station_before_expensive_stretch():
    # Arrive at the $2 station with 200 mi left: fill 30 gal (tank full),
    # then buy only the remaining 20 gal at the $5 station.
    result = plan([(300, 2.0), (600, 5.0)], 1000)
    assert stops_summary(result) == [(300, 30.0), (600, 20.0)]
    assert result.total_cost == pytest.approx(30 * 2 + 20 * 5)


def test_buys_only_enough_to_finish():
    result = plan([(450, 3.0)], 700)
    assert stops_summary(result) == [(450, 20.0)]  # 50 mi left + 20 gal = 250 mi


def test_respects_start_fuel_fraction():
    result = plan([(50, 3.0)], 300, start_fuel_fraction=0.2)  # 100 mi of fuel
    assert stops_summary(result) == [(50, 20.0)]


def test_same_location_duplicates_use_cheapest():
    result = plan([(400, 3.5), (400, 3.1), (400, 3.3)], 700)
    assert [s.station.price for s in result.stops] == [3.1]


def test_gap_longer_than_range_is_infeasible():
    with pytest.raises(InfeasibleRouteError, match="No fuel station between mile 300 and mile 900"):
        plan([(300, 3.0), (900, 3.0)], 1200)


def test_first_station_out_of_reach_is_infeasible():
    with pytest.raises(InfeasibleRouteError, match="first station"):
        plan([(150, 3.0)], 900, start_fuel_fraction=0.2)


def test_no_stations_is_infeasible():
    with pytest.raises(InfeasibleRouteError, match="No fuel stations"):
        plan([], 900)


def test_stop_penalty_avoids_tiny_purchases():
    # Without a penalty it is cheaper (by 45 cents) to fill up at the $3.00
    # station and top up 5 gal at the $3.01 one, 50 miles later.
    stations = [(450, 3.00), (500, 3.01)]
    cheapest = plan(stations, 1000)
    assert len(cheapest.stops) == 2
    practical = plan(stations, 1000, stop_penalty=5)
    assert len(practical.stops) == 1
    assert practical.total_cost - cheapest.total_cost < 0.5  # costs only cents more


def test_never_runs_dry():
    random.seed(7)
    stations = [(m, round(random.uniform(2.8, 4.2), 3)) for m in range(20, 2500, 37)]
    result = plan(stations, 2500, stop_penalty=5)
    fuel, position = 500.0, 0.0
    for stop in result.stops:
        fuel -= stop.station.mile - position
        assert fuel >= -1e-9
        fuel += stop.gallons * 10
        assert fuel <= 500 + 1e-9
        position = stop.station.mile
    assert fuel - (2500 - position) >= -1e-9


# --- Cross-check against the classic greedy algorithm (known optimal) -------

def greedy_reference_cost(stations, route_miles, capacity=500, mpg=10, start=500):
    """Textbook greedy: go to the next cheaper station in reach, else fill up."""
    by_mile = {}
    for m, p in stations:
        if m < route_miles:
            by_mile[m] = min(p, by_mile.get(m, p))
    stops = sorted(by_mile.items())
    miles = [m for m, _ in stops]
    if start >= route_miles:
        return 0.0
    fuel, i, cost = start - miles[0], 0, 0.0
    while True:
        mile, price = stops[i]
        last = bisect_right(miles, mile + capacity) - 1
        cheaper = next((j for j in range(i + 1, last + 1) if stops[j][1] < price), None)
        if cheaper is not None:
            target, nxt = stops[cheaper][0], cheaper
        elif route_miles <= mile + capacity:
            target, nxt = route_miles, None
        else:
            target = mile + capacity
            nxt = min(range(i + 1, last + 1), key=lambda j: stops[j][1])
        need = max(0.0, target - mile - fuel)
        cost += need / mpg * price
        fuel += need
        if nxt is None:
            return cost
        fuel -= stops[nxt][0] - mile
        i = nxt


@pytest.mark.parametrize("seed", range(25))
def test_matches_greedy_optimum_without_stop_penalty(seed):
    rng = random.Random(seed)
    route = rng.randint(600, 3000)
    stations, mile = [], 0
    while True:  # random stations, never more than 450 mi apart
        mile += rng.randint(5, 450)
        if mile >= route:
            break
        stations.append((mile, round(rng.uniform(2.5, 4.5), 3)))
    expected = greedy_reference_cost(stations, route)
    assert plan(stations, route).total_cost == pytest.approx(expected, abs=1e-4)
