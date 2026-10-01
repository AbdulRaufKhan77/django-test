"""Thin OpenRouteService client: geocoding plus one directions call per trip.

Every response is cached, so a repeated trip costs zero external calls. The
client counts the real HTTP calls it makes so the API can report them.
"""

import hashlib
import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

GEOCODE_LAYERS = "venue,address,street,locality,localadmin,borough,neighbourhood,postalcode,county"


class RoutingError(Exception):
    """The trip can't be routed (bad input, unreachable, too long...). Client's fault."""


class RoutingServiceError(Exception):
    """ORS is unreachable, rate-limited or misconfigured. Our side's fault."""


@dataclass(frozen=True)
class Place:
    label: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Route:
    coordinates: list  # [[lon, lat], ...] as returned by ORS (GeoJSON order)
    distance_meters: float
    duration_seconds: float


class OrsClient:
    def __init__(self, api_key=None, base_url=None, profile=None, timeout=None):
        self.api_key = api_key if api_key is not None else settings.ORS_API_KEY
        self.base_url = (base_url or settings.ORS_BASE_URL).rstrip("/")
        self.profile = profile or settings.ORS_PROFILE
        self.timeout = timeout or settings.ORS_TIMEOUT_SECONDS
        self.calls_made = 0
        self.cache_hits = 0
        self._session = requests.Session()

    # -- public API ----------------------------------------------------------------

    def geocode(self, text: str) -> Place:
        key = self._cache_key("geocode", " ".join(text.lower().split()))
        if (hit := cache.get(key)) is not None:
            self.cache_hits += 1
            return hit
        data = self._request(
            "GET",
            "/geocode/search",
            params={
                "api_key": self.api_key,
                "text": text,
                "boundary.country": "US",
                "layers": GEOCODE_LAYERS,
                "size": 1,
            },
        )
        features = data.get("features") or []
        if not features:
            raise RoutingError(f"Could not find a US location matching '{text}'.")
        feature = features[0]
        props = feature.get("properties", {})
        if props.get("country_a") not in (None, "USA"):
            raise RoutingError(f"'{text}' is not in the USA.")
        lon, lat = feature["geometry"]["coordinates"]
        place = Place(label=props.get("label") or text, lat=lat, lon=lon)
        cache.set(key, place)
        return place

    def directions(self, start: Place, finish: Place) -> Route:
        coords = [[round(start.lon, 5), round(start.lat, 5)], [round(finish.lon, 5), round(finish.lat, 5)]]
        key = self._cache_key("route", self.profile, repr(coords))
        if (hit := cache.get(key)) is not None:
            self.cache_hits += 1
            return hit
        data = self._request(
            "POST",
            f"/v2/directions/{self.profile}/geojson",
            json={"coordinates": coords, "instructions": False},
            headers={"Authorization": self.api_key},
        )
        try:
            feature = data["features"][0]
            summary = feature["properties"]["summary"]
            route = Route(
                coordinates=feature["geometry"]["coordinates"],
                distance_meters=float(summary.get("distance", 0.0)),
                duration_seconds=float(summary.get("duration", 0.0)),
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise RoutingServiceError("Unexpected response from routing service.") from exc
        if len(route.coordinates) < 2:
            raise RoutingError("Start and finish are the same place.")
        cache.set(key, route)
        return route

    # -- internals -----------------------------------------------------------------

    @staticmethod
    def _cache_key(*parts: str) -> str:
        return "ors:" + hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _request(self, method, path, **kwargs):
        if not self.api_key:
            raise RoutingServiceError("ORS_API_KEY is not configured.")
        self.calls_made += 1
        try:
            resp = self._session.request(
                method, self.base_url + path, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            logger.warning("ORS request failed: %s", exc)
            raise RoutingServiceError("Routing service is unreachable.") from exc

        if resp.status_code == 200:
            return resp.json()
        try:
            error = resp.json().get("error", {})
        except ValueError:
            error = {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        logger.warning("ORS %s %s -> %s %s", method, path, resp.status_code, message)
        if resp.status_code in (400, 404):
            # e.g. code 2010 "could not find routable point", 2004 "route too long"
            raise RoutingError(message or "Route could not be calculated.")
        if resp.status_code in (401, 403):
            raise RoutingServiceError("Routing service rejected the API key.")
        if resp.status_code == 429:
            raise RoutingServiceError("Routing service rate limit reached; try again later.")
        raise RoutingServiceError(f"Routing service error ({resp.status_code}).")
