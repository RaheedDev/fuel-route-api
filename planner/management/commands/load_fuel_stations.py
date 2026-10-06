"""
Import the fuel-price CSV into the database, geocoding every station OFFLINE.

    python manage.py load_fuel_stations    # data/fuel-prices-for-be-assessment.csv
    python manage.py load_fuel_stations path/to/other.csv

What it does:
* skips non-US rows (the file also lists Canadian provinces),
* de-duplicates by OPIS Truckstop ID, keeping the LOWEST price (678 IDs
  appear 2-6 times, often with slightly different names/prices; we assume
  a driver would pay the best listed price at that station),
* gives each station the centroid of its City + State from the bundled
  Census/USGS gazetteer - no API calls at all,
* replaces the previous contents of the table in one transaction.
"""

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import FuelStation
from planner.services import gazetteer
from planner.services.stations import mark_stations_changed

REQUIRED_COLUMNS = {"OPIS Truckstop ID", "Truckstop Name", "City", "State", "Retail Price"}


class Command(BaseCommand):
    help = "Load fuel stations + prices from the CSV and geocode them offline."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", default=str(settings.FUEL_PRICES_CSV))

    def handle(self, *args, csv_path, **options):
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")

        stations, stats = self.read_csv(path)
        located = self.geocode(stations)

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values(), batch_size=1000)
        mark_stations_changed()

        unlocated = [s for s in stations.values() if not s.has_location]
        self.stdout.write(self.style.SUCCESS(
            f"Read {stats['rows']} rows -> {len(stations)} unique US stations "
            f"({stats['duplicates']} duplicate rows merged, {stats['non_us']} non-US rows "
            f"and {stats['invalid']} invalid rows skipped).\n"
            f"Located {located}/{len(stations)} stations "
            f"({100 * located / max(len(stations), 1):.1f}%) from City+State."
        ))
        if unlocated:
            sample = ", ".join(sorted({f"{s.city}, {s.state}" for s in unlocated})[:15])
            self.stdout.write(self.style.WARNING(
                f"{len(unlocated)} stations kept without coordinates (not used for routing): {sample}..."
            ))

    def read_csv(self, path):
        stats = {"rows": 0, "duplicates": 0, "non_us": 0, "invalid": 0}
        stations = {}  # opis_id -> FuelStation (unsaved)
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            reader.fieldnames = [name.strip() for name in reader.fieldnames or []]
            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise CommandError(f"CSV is missing columns: {', '.join(sorted(missing))}")

            for row in reader:
                stats["rows"] += 1
                row = {k: (v or "").strip() for k, v in row.items() if k}
                state = row["State"].upper()
                if state not in gazetteer.US_STATES:
                    stats["non_us"] += 1
                    continue
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = Decimal(row["Retail Price"]).quantize(Decimal("0.00001"))
                except (ValueError, InvalidOperation):
                    stats["invalid"] += 1
                    continue
                if price <= 0:
                    stats["invalid"] += 1
                    continue

                existing = stations.get(opis_id)
                if existing is not None:
                    stats["duplicates"] += 1
                    existing.source_rows += 1
                    if price < existing.price:
                        existing.price = price
                    continue
                stations[opis_id] = FuelStation(
                    opis_id=opis_id,
                    name=row["Truckstop Name"],
                    address=row.get("Address", ""),
                    city=row["City"],
                    state=state,
                    rack_id=int(row["Rack ID"]) if row.get("Rack ID", "").isdigit() else None,
                    price=price,
                )
        return stations, stats

    def geocode(self, stations):
        located = 0
        for station in stations.values():
            place = gazetteer.find_place(station.city, station.state)
            if place:
                station.latitude, station.longitude = place.lat, place.lng
                located += 1
        return located
