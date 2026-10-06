from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStation
from .serializers import RoutePlanRequestSerializer
from .services.errors import PlannerError
from .services.planner import PlanOptions, plan_trip


def _error(code, message, status, **extra):
    return Response({"error": {"code": code, "message": message, **extra}}, status=status)


def _options(data):
    return PlanOptions(
        start_fuel=data["start_fuel"],
        corridor_miles=data.get("corridor_miles"),
        stop_penalty=data.get("stop_penalty"),
        geometry=data["geometry"],
    )


def _map_url(request, data):
    """Link to the HTML map for the same trip (the route is cached by then)."""
    params = {"start": data["start"], "finish": data["finish"]}
    if data["start_fuel"] != 1.0:
        params["start_fuel"] = data["start_fuel"]
    for optional in ("corridor_miles", "stop_penalty"):
        if data.get(optional) is not None:
            params[optional] = data[optional]
    return request.build_absolute_uri(reverse("route-map") + "?" + urlencode(params))


class RoutePlanView(APIView):
    """
    Plan a trip: route + cost-optimal fuel stops + total fuel cost.

    GET  /api/route/?start=New York, NY&finish=Los Angeles, CA
    POST /api/route/  {"start": "New York, NY", "finish": "Los Angeles, CA"}

    Optional: start_fuel (0-1, default 1 = full tank), corridor_miles (1-50,
    default 10), stop_penalty (USD, default 5), geometry (simplified | full | none).
    """

    def get(self, request):
        return self.plan(request, request.query_params)

    def post(self, request):
        return self.plan(request, request.data)

    def plan(self, request, raw):
        serializer = RoutePlanRequestSerializer(data=raw)
        if not serializer.is_valid():
            return _error("invalid_request", "Invalid parameters.", 400, fields=serializer.errors)
        data = serializer.validated_data
        try:
            result = plan_trip(data["start"], data["finish"], _options(data))
        except PlannerError as exc:
            return _error(exc.code, exc.message, exc.status_code)
        return Response({"map_url": _map_url(request, data), **result})


def route_map(request):
    """
    HTML page with a Leaflet/OpenStreetMap map of the route and fuel stops.
    Takes the same query parameters as GET /api/route/.
    """
    serializer = RoutePlanRequestSerializer(data=request.GET)
    if not serializer.is_valid():
        return render(request, "planner/map.html", {"error": serializer.errors}, status=400)
    data = serializer.validated_data
    try:
        plan = plan_trip(data["start"], data["finish"], _options({**data, "geometry": "full"}))
    except PlannerError as exc:
        return render(request, "planner/map.html", {"error": exc.message}, status=exc.status_code)
    return render(request, "planner/map.html", {"plan": plan})


class HealthView(APIView):
    def get(self, request):
        return Response({
            "status": "ok",
            "stations": FuelStation.objects.count(),
            "stations_with_location": FuelStation.objects.filter(latitude__isnull=False).count(),
        })


class ApiRootView(APIView):
    def get(self, request):
        example = "?" + urlencode({"start": "New York, NY", "finish": "Los Angeles, CA"})
        return Response({
            "plan_route": request.build_absolute_uri(reverse("route-plan") + example),
            "route_map": request.build_absolute_uri(reverse("route-map") + example),
            "health": request.build_absolute_uri(reverse("health")),
        })
