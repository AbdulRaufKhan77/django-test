from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from planner.models import FuelStation
from planner.services import stations
from planner.services.ors import Place, Route, RoutingServiceError

# A straight 1000-mile-ish route due east along latitude 40.
ROUTE = Route(
    coordinates=[[-110.0 + i * 0.2, 40.0] for i in range(96)],
    distance_meters=1000 * 1609.344,
    duration_seconds=15 * 3600,
)
DUMMY_CACHE = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}


class FakeOrs:
    """Stands in for OrsClient; counts calls like the real one."""

    def __init__(self, *a, **kw):
        self.calls_made = 0
        self.cache_hits = 0

    def geocode(self, text):
        self.calls_made += 1
        return {
            "Start, UT": Place("Start, UT, USA", 40.0, -110.0),
            "Finish, NE": Place("Finish, NE, USA", 40.0, -91.0),
        }[text]

    def directions(self, start, finish):
        self.calls_made += 1
        return ROUTE


@override_settings(CACHES=DUMMY_CACHE)
@mock.patch("planner.services.planner.OrsClient", FakeOrs)
class RoutePlanApiTests(TestCase):
    def setUp(self):
        stations.reset_table()
        self.client = APIClient()
        for i, (lon, price) in enumerate([(-102.0, "3.50"), (-100.5, "2.80"), (-96.0, "3.20"), (-94.0, "3.90")]):
            FuelStation.objects.create(
                opis_id=i + 1, name=f"Stop {i + 1}", address="I-80", city="Town", state="NE",
                price=price, latitude=40.02, longitude=lon, geocode_source="test",
            )

    def tearDown(self):
        stations.reset_table()

    def test_get_returns_plan(self):
        resp = self.client.get("/api/route/", {"start": "Start, UT", "finish": "Finish, NE"})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["route"]["distance_miles"], 1000.0)
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertGreater(body["summary"]["number_of_stops"], 0)
        self.assertAlmostEqual(
            body["summary"]["total_fuel_cost"],
            round(sum(s["cost"] for s in body["fuel_stops"]), 2),
            places=1,
        )
        self.assertEqual(body["summary"]["gallons_used"], 100.0)
        self.assertEqual(body["meta"]["routing_api_calls"], 3)  # 2 geocodes + 1 route
        self.assertIn("/map/?", body["map_url"])

    def test_coordinates_skip_geocoding(self):
        resp = self.client.post(
            "/api/route/",
            {"start": {"lat": 40.0, "lon": -110.0}, "finish": "40.0,-91.0"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["meta"]["routing_api_calls"], 1)

    def test_geojson_format(self):
        resp = self.client.get(
            "/api/route/", {"start": "Start, UT", "finish": "Finish, NE", "format": "geojson"}
        )
        body = resp.json()
        self.assertEqual(body["type"], "FeatureCollection")
        kinds = [f["properties"]["kind"] for f in body["features"]]
        self.assertEqual(kinds[:3], ["route", "start", "finish"])
        self.assertIn("fuel_stop", kinds)

    def test_missing_params(self):
        resp = self.client.get("/api/route/", {"start": "Start, UT"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("finish", resp.json()["errors"])

    def test_outside_usa_rejected(self):
        resp = self.client.get("/api/route/", {"start": "51.5,-0.12", "finish": "Finish, NE"})
        self.assertEqual(resp.status_code, 422)
        self.assertIn("outside the USA", resp.json()["error"])

    def test_unreachable_gap_is_422(self):
        resp = self.client.get(
            "/api/route/",
            {"start": "Start, UT", "finish": "Finish, NE", "start_fuel_gallons": 1},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Starting fuel only covers", resp.json()["error"])

    def test_routing_service_failure_is_502(self):
        with mock.patch.object(FakeOrs, "directions", side_effect=RoutingServiceError("down")):
            resp = self.client.get("/api/route/", {"start": "Start, UT", "finish": "Finish, NE"})
        self.assertEqual(resp.status_code, 502)

    def test_map_page_renders(self):
        resp = self.client.get("/map/", {"start": "Start, UT", "finish": "Finish, NE"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "trip-data")
        self.assertContains(resp, "Total fuel cost")
