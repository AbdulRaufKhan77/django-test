"""Trip planning: locations in, route + cheapest fuel stops + total cost out."""

import re
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from . import geo, stations
from .optimizer import Candidate, InfeasibleRoute, plan_fuel_stops, prune_dominated
from .ors import OrsClient, Place, RoutingError

# Rough boxes for the contiguous US, Alaska and Hawaii. Only used to reject
# obviously foreign coordinates; text input is restricted to the US by the geocoder.
US_BOXES = (
    (24.3, 49.5, -125.0, -66.8),
    (51.0, 71.6, -180.0, -129.9),
    (18.8, 22.4, -160.6, -154.7),
)
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class TripRequest:
    start: str | dict
    finish: str | dict
    max_detour_miles: float | None = None
    start_fuel_gallons: float | None = None  # None = full tank


def in_usa(lat: float, lon: float) -> bool:
    return any(a <= lat <= b and c <= lon <= d for a, b, c, d in US_BOXES)


def resolve_location(value, client: OrsClient) -> Place:
    """Accept {"lat":..,"lon":..}, "lat,lon", or free text like "Dallas, TX".

    Coordinates skip geocoding entirely, so a trip given as coordinates costs
    exactly one routing call.
    """
    if isinstance(value, dict):
        lat, lon = value.get("lat"), value.get("lon", value.get("lng"))
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise RoutingError("Coordinates need numeric 'lat' and 'lon'.") from None
        label = value.get("label") or f"{lat:.5f},{lon:.5f}"
    elif isinstance(value, str) and (m := _LATLON.match(value)):
        lat, lon = float(m.group(1)), float(m.group(2))
        label = value.strip()
    elif isinstance(value, str) and value.strip():
        place = client.geocode(value.strip())
        lat, lon, label = place.lat, place.lon, place.label
    else:
        raise RoutingError("Location must be an address/place name or 'lat,lon'.")

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RoutingError(f"Invalid coordinates: {lat}, {lon}.")
    if not in_usa(lat, lon):
        raise RoutingError(f"'{label}' is outside the USA.")
    return Place(label=label, lat=lat, lon=lon)


def plan_trip(req: TripRequest, client: OrsClient | None = None) -> dict:
    t0 = time.perf_counter()
    cfg = settings.FUEL_PLANNER
    client = client or OrsClient()
    range_miles, mpg = float(cfg["VEHICLE_RANGE_MILES"]), float(cfg["MILES_PER_GALLON"])
    capacity = range_miles / mpg
    detour = cfg["DEFAULT_MAX_DETOUR_MILES"] if req.max_detour_miles is None else req.max_detour_miles

    start = resolve_location(req.start, client)
    finish = resolve_location(req.finish, client)
    route = client.directions(start, finish)

    coords = np.asarray(route.coordinates, dtype=float)
    lon, lat = coords[:, 0], coords[:, 1]
    total_miles = route.distance_meters / geo.METERS_PER_MILE
    polyline_miles = float(geo.cumulative_miles(lat, lon)[-1]) or 1.0
    scale = total_miles / polyline_miles  # align mile markers with ORS's road distance

    along = stations.stations_along_route(lat, lon, detour)
    candidates = [Candidate(key=s, mile=s.mile * scale, price=s.price) for s in along]
    def solve(cands):
        return plan_fuel_stops(
            cands,
            total_miles,
            range_miles=range_miles,
            mpg=mpg,
            start_fuel_gallons=req.start_fuel_gallons,
            min_purchase_gallons=float(cfg["MIN_PURCHASE_GALLONS"]),
        )

    try:
        try:
            plan = solve(prune_dominated(candidates, float(cfg["CLUSTER_WINDOW_MILES"])))
        except InfeasibleRoute:
            plan = solve(candidates)  # pruning opened a gap; use every station
    except InfeasibleRoute as exc:
        raise RoutingError(
            f"{exc} Try a larger max_detour_miles (currently {detour:g})."
        ) from exc

    records = stations.get_table().records
    stops = []
    for n, p in enumerate(plan.purchases, start=1):
        rec = records[p.key.opis_id]
        stops.append(
            {
                "stop": n,
                "station": {
                    "opis_id": rec["opis_id"],
                    "name": rec["name"],
                    "address": rec["address"],
                    "city": rec["city"],
                    "state": rec["state"],
                    "lat": rec["latitude"],
                    "lon": rec["longitude"],
                },
                "price_per_gallon": round(p.price, 3),
                "mile_marker": round(p.mile, 1),
                "off_route_miles": round(p.key.off_route_miles, 1),
                "fuel_on_arrival_gallons": round(p.fuel_on_arrival, 2),
                "gallons": round(p.gallons, 2),
                "cost": round(p.cost, 2),
            }
        )

    display_coords = geo.thin_polyline(route.coordinates, cfg["MAX_GEOMETRY_POINTS"])
    total_cost = plan.total_cost
    gallons_bought = plan.gallons_purchased

    return {
        "start": {"query": req.start, "label": start.label, "lat": start.lat, "lon": start.lon},
        "finish": {"query": req.finish, "label": finish.label, "lat": finish.lat, "lon": finish.lon},
        "route": {
            "distance_miles": round(total_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2),
            "geometry": {"type": "LineString", "coordinates": display_coords},
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": round(total_cost, 2),
            "gallons_purchased": round(gallons_bought, 2),
            "gallons_used": round(plan.gallons_used, 2),
            "average_price_paid": round(total_cost / gallons_bought, 3) if gallons_bought else None,
            "number_of_stops": len(stops),
            "start_fuel_gallons": round(plan.start_fuel, 2),
            "fuel_at_destination_gallons": round(plan.fuel_at_destination, 2),
            "stations_considered": len(candidates),
        },
        "assumptions": {
            "vehicle_range_miles": range_miles,
            "miles_per_gallon": mpg,
            "tank_capacity_gallons": capacity,
            "starts_with": "full tank" if req.start_fuel_gallons is None else f"{plan.start_fuel:g} gal",
            "max_detour_miles": detour,
            "notes": [
                "Fuel already in the tank at the start is not counted in total_fuel_cost.",
                "Stations are placed at their town's coordinates; the source data has no exact locations.",
                "Detour distance to reach a station is not added to the trip.",
                f"Within any {cfg['CLUSTER_WINDOW_MILES']} mi stretch only the cheapest station is considered.",
                f"Purchases under {cfg['MIN_PURCHASE_GALLONS']} gal are skipped when the next station is still reachable.",
            ],
        },
        "meta": {
            "routing_api_calls": client.calls_made,
            "cache_hits": client.cache_hits,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }


def to_geojson(result: dict) -> dict:
    """FeatureCollection of the route line plus one point per fuel stop."""
    features = [
        {
            "type": "Feature",
            "geometry": result["route"]["geometry"],
            "properties": {"kind": "route", "distance_miles": result["route"]["distance_miles"]},
        }
    ]
    for end in ("start", "finish"):
        p = result[end]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": {"kind": end, "label": p["label"]},
            }
        )
    for s in result["fuel_stops"]:
        st = s["station"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
                "properties": {"kind": "fuel_stop", **{k: v for k, v in s.items() if k != "station"}, **st},
            }
        )
    return {"type": "FeatureCollection", "features": features}
