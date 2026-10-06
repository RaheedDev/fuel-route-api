from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    """Validates query params (GET) or JSON body (POST) for a trip plan."""

    start = serializers.CharField(
        max_length=200, help_text="'City, ST', 5-digit ZIP, 'lat,lng' or an address"
    )
    finish = serializers.CharField(max_length=200)
    start_fuel = serializers.FloatField(
        default=1.0, min_value=0.0, max_value=1.0,
        help_text="Fraction of a full tank at departure (1 = full, the default).",
    )
    corridor_miles = serializers.FloatField(
        required=False, min_value=1.0, max_value=50.0,
        help_text="Max distance of a station from the route (default 10).",
    )
    stop_penalty = serializers.FloatField(
        required=False, min_value=0.0, max_value=200.0,
        help_text="USD 'cost' per extra stop used when choosing stops (default 5; 0 = cheapest).",
    )
    geometry = serializers.ChoiceField(
        choices=["simplified", "full", "none"], default="simplified",
        help_text="Route line detail in the GeoJSON output.",
    )
