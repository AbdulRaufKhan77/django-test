import logging
from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from planner.models import FuelStation

from .serializers import TripRequestSerializer
from .services.ors import RoutingError, RoutingServiceError
from .services.planner import TripRequest, plan_trip, to_geojson

logger = logging.getLogger(__name__)


def _run(params):
    """Validate params and plan the trip. Returns (result, error_response)."""
    serializer = TripRequestSerializer(data=params)
    if not serializer.is_valid():
        return None, Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data
    try:
        result = plan_trip(
            TripRequest(
                start=data["start"],
                finish=data["finish"],
                max_detour_miles=data.get("max_detour_miles"),
                start_fuel_gallons=data.get("start_fuel_gallons"),
            )
        )
    except RoutingError as exc:
        return None, Response({"error": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    except RoutingServiceError as exc:
        return None, Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return (result, data), None


def _map_url(request, data):
    query = {"start": data["start"], "finish": data["finish"]}
    for key in ("max_detour_miles", "start_fuel_gallons"):
        if key in data:
            query[key] = data[key]
    if any(isinstance(v, dict) for v in query.values()):
        query = {
            k: (f"{v['lat']},{v.get('lon', v.get('lng'))}" if isinstance(v, dict) else v)
            for k, v in query.items()
        }
    return request.build_absolute_uri(reverse("route-map") + "?" + urlencode(query))


class RoutePlanView(APIView):
    """Plan a trip: route, cheapest fuel stops (500 mi range), total fuel cost at 10 mpg.

    GET  /api/route/?start=Chicago, IL&finish=Denver, CO
    POST /api/route/  {"start": "Chicago, IL", "finish": {"lat": 39.74, "lon": -104.99}}
    Optional: max_detour_miles, start_fuel_gallons, format=geojson
    """

    def get(self, request):
        return self._handle(request, request.query_params.dict())

    def post(self, request):
        params = dict(request.data) if isinstance(request.data, dict) else {}
        params.setdefault("format", request.query_params.get("format", "json"))
        return self._handle(request, params)

    def _handle(self, request, params):
        ok, error = _run(params)
        if error:
            return error
        result, data = ok
        if data.get("format") == "geojson":
            return Response(to_geojson(result))
        return Response({**result, "map_url": _map_url(request, data)})


class HealthView(APIView):
    def get(self, request):
        return Response({"status": "ok", "stations": FuelStation.objects.count()})


def route_map(request):
    """Leaflet map of a planned trip. Reuses the cached route, so it costs no extra
    routing calls after the API request that produced its link."""
    ok, error = _run(request.GET.dict())
    if error:
        return render(request, "planner/map.html", {"error": error.data}, status=error.status_code)
    result, _ = ok
    return render(
        request,
        "planner/map.html",
        {"result": result, "geojson": to_geojson(result)},
    )
