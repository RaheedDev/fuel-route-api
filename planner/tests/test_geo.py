import numpy as np
import pytest

from planner.services import gazetteer
from planner.services.errors import GeocodingError
from planner.services.geocoding import geocode
from planner.services.geometry import (
    cumulative_miles,
    decode_polyline,
    densify,
    haversine_miles,
)


def test_haversine_new_york_to_los_angeles():
    assert haversine_miles(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(2445, abs=5)


def test_decode_polyline_reference_example():
    # Example from Google's polyline format documentation.
    coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    expected = [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]
    np.testing.assert_allclose(coords, expected, atol=1e-9)


def test_densify_limits_segment_length_and_keeps_endpoints():
    line = np.array([[40.0, -100.0], [40.0, -99.0]])  # ~53 miles
    dense = densify(line, 0.5)
    steps = np.diff(cumulative_miles(dense))
    assert steps.max() <= 0.5 + 1e-9
    assert dense[0].tolist() == line[0].tolist() and dense[-1].tolist() == line[-1].tolist()


@pytest.mark.parametrize(
    "city, state, expected_name",
    [
        ("St. Louis", "MO", "St. Louis"),
        ("Saint Louis", "MO", "St. Louis"),
        ("Ft Worth", "tx", "Fort Worth"),
        ("Mc Calla", "AL", "McCalla"),
        ("Nashville", "Tennessee", "Nashville"),
        ("Big Cabin", "OK", "Big Cabin"),
    ],
)
def test_gazetteer_name_variants(city, state, expected_name):
    place = gazetteer.find_place(city, state)
    assert place is not None and place.name == expected_name


def test_gazetteer_unknown_place():
    assert gazetteer.find_place("Atlantis", "TX") is None
    assert gazetteer.find_place("Toronto", "ON") is None  # not a US state


@pytest.mark.parametrize(
    "query, source",
    [
        ("40.7128,-74.0060", "coordinates"),
        ("Chicago, IL", "gazetteer"),
        ("Salt Lake City Utah", "gazetteer"),
        ("100 Main St, Dallas, TX 75201", "gazetteer"),
        ("10001", "zip"),
    ],
)
def test_geocode_offline_formats(query, source):
    location = geocode(query)
    assert location.source == source
    assert location.api_call is False
    assert gazetteer.is_in_usa(location.lat, location.lng)


@pytest.mark.parametrize("query", ["43.6532,-79.3832", "19.4326,-99.1332"])  # Toronto, Mexico City
def test_geocode_rejects_points_outside_usa(query):
    with pytest.raises(GeocodingError) as exc:
        geocode(query)
    assert exc.value.code == "outside_usa"


def test_geocode_unknown_place_without_online_fallback():
    with pytest.raises(GeocodingError, match="Could not find"):
        geocode("Definitely Not A Real Place")


def test_nominatim_fallback_is_called_once_and_cached(settings):
    from unittest import mock

    from planner.services import geocoding

    settings.NOMINATIM_URL = "https://nominatim.example"
    fake = mock.Mock(status_code=200)
    fake.json.return_value = [{"lat": "38.8977", "lon": "-77.0365", "display_name": "The White House"}]
    fake.raise_for_status.return_value = None
    with mock.patch.object(geocoding._session, "get", return_value=fake) as get:
        first = geocode("1600 Pennsylvania Avenue NW, Washington")
        second = geocode("1600 Pennsylvania Avenue NW, Washington")
    assert get.call_count == 1
    assert first.source == "nominatim" and first.api_call is True
    assert second.api_call is False and second.lat == first.lat
