from decimal import Decimal

import pytest
from django.core.management import call_command

from planner.models import FuelStation

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,931,3.859
55,FLYING J,"HWY 1",Calgary,AB,1,1.50
56,MYSTERY STOP,"I-1",Atlantis,TX,2,3.10
57,BAD PRICE,"I-2",Dallas,TX,3,n/a
"""


@pytest.mark.django_db
def test_load_fuel_stations(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(CSV)
    call_command("load_fuel_stations", str(path))

    assert set(FuelStation.objects.values_list("opis_id", flat=True)) == {7, 20, 56}
    pilot = FuelStation.objects.get(opis_id=20)
    assert pilot.price == Decimal("3.85900")  # duplicate rows -> lowest price kept
    assert pilot.source_rows == 2
    assert pilot.has_location and pilot.latitude == pytest.approx(32.95, abs=0.1)
    assert FuelStation.objects.get(opis_id=56).has_location is False  # unknown town


@pytest.mark.django_db
def test_load_replaces_previous_data(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(CSV)
    call_command("load_fuel_stations", str(path))
    call_command("load_fuel_stations", str(path))
    assert FuelStation.objects.count() == 3
