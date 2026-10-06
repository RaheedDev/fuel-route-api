from django.db import models


class FuelStation(models.Model):
    """
    A truck stop from the provided fuel-price CSV.

    The CSV has no coordinates, so ``latitude``/``longitude`` are the centroid
    of the station's City + State, resolved offline by the
    ``load_fuel_stations`` command. Stations whose town could not be resolved
    are kept (with null coordinates) but never used for routing.
    """

    opis_id = models.PositiveIntegerField(unique=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price = models.DecimalField(
        max_digits=8, decimal_places=5, help_text="Retail price, USD per gallon"
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # How many CSV rows shared this OPIS ID (the file contains duplicates).
    source_rows = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["state", "city", "name"]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None
