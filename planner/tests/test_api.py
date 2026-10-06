"""
End-to-end tests of the HTTP API with the routing service mocked out.

The fake OSRM returns a straight line Chicago -> Dallas (~800 mi) and we
place stations on that line, so results are deterministic and offline.
"""

from unittest import mock

import numpy as np
import pytest
from rest_framework.test import APIClient

from planner.models import FuelStation
from planner.services import routing
from planner.services.geometry import haversine_miles
from planner.services.stations import mark_stations_changed

CHICAGO = (41.8781, -87.6298)
DALLAS = (32.7767, -96.7970)
LINE = np.linspace(CHICAGO, DALLAS, 400)
LINE_MILES = float(haversine_miles(*CHICAGO, *DALLAS))


def encode_polyline(coords, precision=5):
    """Minimal encoder (inverse of geometry.decode_polyline) for the fake OSRM."""
    out, prev = [], (0, 0)
    for lat, lng in coords:
        point = (round(lat * 10**precision), round(lng * 10**precision))
        for value in (point[0] - prev[0], point[1] - prev[1]):
            value = ~(value << 1) if value < 0 else value << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev = point
    return "".join(out)


def fake_osrm_response(code="Ok", status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = {
        "code": code,
        "routes": [] if code != "Ok" else [{
            "geometry": encode_polyline(LINE),
            "distance": LINE_MILES * routing.METERS_PER_MILE,
            "duration": LINE_MILES / 60 * 3600,
        }],
    }
    return response


@pytest.fixture
def osrm():
    with mock.patch.object(routing._session, "get", return_value=fake_osrm_response()) as get:
        yield get


@pytest.fixture
def stations(db):
    """Four stations on the line at 25/50/62/80 % of the way, plus one far away."""
    def at(fraction):
        return CHICAGO[0] + fraction * (DALLAS[0] - CHICAGO[0]), CHICAGO[1] + fraction * (DALLAS[1] - CHICAGO[1])

    specs = [(1, 0.25, "3.50"), (2, 0.50, "3.10"), (3, 0.62, "2.90"), (4, 0.80, "3.40")]
    for opis_id, fraction, price in specs:
        lat, lng = at(fraction)
        FuelStation.objects.create(opis_id=opis_id, name=f"Stop {opis_id}", address="I-44",
                                   city="Town", state="MO", price=price, latitude=lat, longitude=lng)
    FuelStation.objects.create(opis_id=99, name="Far away", city="Miami", state="FL",
                               price="1.00", latitude=25.76, longitude=-80.19)
    mark_stations_changed()


@pytest.fixture
def client():
    return APIClient()


def test_plan_route_get(client, osrm, stations):
    response = client.get("/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX", "start_fuel": 0.5})
    assert response.status_code == 200, response.content
    body = response.json()

    assert body["start"]["resolved_by"] == "gazetteer"
    assert body["route"]["distance_miles"] == pytest.approx(LINE_MILES, abs=0.5)
    assert body["summary"]["stations_considered"] == 4  # Miami is outside the corridor
    # 250 mi of starting fuel -> must stop at the 25 % station (~200 mi), then the
    # optimiser prefers the cheapest station (62 %) for the rest of the trip.
    assert [s["opis_id"] for s in body["fuel_stops"]] == [1, 3]
    gallons = body["summary"]["total_gallons_purchased"]
    assert gallons == pytest.approx((LINE_MILES - 250) / 10, abs=0.2)
    assert body["summary"]["total_fuel_cost"] == pytest.approx(sum(s["cost"] for s in body["fuel_stops"]), abs=0.02)
    assert body["map_url"].startswith("http://testserver/api/route/map/?")
    assert body["geojson"]["features"][0]["geometry"]["type"] == "LineString"
    assert body["meta"]["external_api_calls"] == {"routing": 1, "geocoding": 0, "total": 1}


def test_second_request_is_served_from_cache(client, osrm, stations):
    params = {"start": "Chicago, IL", "finish": "Dallas, TX"}
    client.get("/api/route/", params)
    body = client.get("/api/route/", params).json()
    assert osrm.call_count == 1  # exactly one routing API call for both requests
    assert body["meta"]["route_cache_hit"] is True
    assert body["meta"]["external_api_calls"]["total"] == 0


def test_plan_route_post_json(client, osrm, stations):
    response = client.post("/api/route/", {"start": "41.8781,-87.6298", "finish": "75201"}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["finish"]["resolved_by"] == "zip"


def test_full_tank_short_trip_needs_no_stops(client, osrm, stations):
    body = client.get("/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX",
                                      "start_fuel": 1}).json()
    # ~800 mi on a full tank (500 mi): one stop, at the cheapest reachable station.
    assert [s["opis_id"] for s in body["fuel_stops"]] == [3]


@pytest.mark.parametrize(
    "params, status, code",
    [
        ({"finish": "Dallas, TX"}, 400, "invalid_request"),
        ({"start": "Chicago, IL", "finish": "Dallas, TX", "start_fuel": 2}, 400, "invalid_request"),
        ({"start": "Chicago, IL", "finish": "43.6532,-79.3832"}, 400, "outside_usa"),
        ({"start": "Chicago, IL", "finish": "Nowhere Land"}, 400, "location_not_found"),
        ({"start": "Chicago, IL", "finish": "Chicago, IL"}, 400, "same_location"),
    ],
)
def test_validation_errors(client, osrm, stations, params, status, code):
    response = client.get("/api/route/", params)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    osrm.assert_not_called()


def test_unroutable(client, stations):
    with mock.patch.object(routing._session, "get", return_value=fake_osrm_response("NoRoute", 400)):
        response = client.get("/api/route/", {"start": "Chicago, IL", "finish": "Honolulu, HI"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_route"


def test_routing_service_down(client, stations):
    import requests

    with mock.patch.object(routing._session, "get", side_effect=requests.ConnectionError("boom")):
        response = client.get("/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_no_station_coverage(client, osrm, db):
    mark_stations_changed()  # empty station table
    response = client.get("/api/route/", {"start": "Chicago, IL", "finish": "Dallas, TX"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "no_fuel_coverage"


def test_map_page(client, osrm, stations):
    response = client.get("/api/route/map/", {"start": "Chicago, IL", "finish": "Dallas, TX"})
    assert response.status_code == 200
    html = response.content.decode()
    assert "leaflet" in html and 'id="route-geojson"' in html and "Stop 3" in html


def test_health(client, stations):
    assert client.get("/api/health/").json()["stations_with_location"] == 5
