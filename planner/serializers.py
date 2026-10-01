from django.conf import settings
from rest_framework import serializers


class LocationField(serializers.Field):
    """A place name/address string, a "lat,lon" string, or {"lat": .., "lon": ..}."""

    default_error_messages = {"invalid": "Give a place name, 'lat,lon', or {\"lat\", \"lon\"}."}

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip():
            return data.strip()
        if isinstance(data, dict) and "lat" in data and ("lon" in data or "lng" in data):
            return data
        self.fail("invalid")

    def to_representation(self, value):
        return value


class TripRequestSerializer(serializers.Serializer):
    start = LocationField()
    finish = LocationField()
    max_detour_miles = serializers.FloatField(
        required=False,
        min_value=0.5,
        max_value=settings.FUEL_PLANNER["MAX_DETOUR_MILES_LIMIT"],
    )
    start_fuel_gallons = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=settings.FUEL_PLANNER["VEHICLE_RANGE_MILES"]
        / settings.FUEL_PLANNER["MILES_PER_GALLON"],
        help_text="Fuel in the tank at the start. Omit for a full tank.",
    )
    format = serializers.ChoiceField(choices=["json", "geojson"], default="json", required=False)
