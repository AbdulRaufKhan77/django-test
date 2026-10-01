import numpy as np
from django.test import TestCase, SimpleTestCase

from planner.models import FuelStation
from planner.services import geo, stations


class GeoTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # Chicago -> Denver is ~920 mi as the crow flies.
        d = geo.haversine_miles(41.8781, -87.6298, 39.7392, -104.9903)
        self.assertAlmostEqual(float(d), 920, delta=5)

    def test_densify_caps_step_and_keeps_endpoints(self):
        lat, lon = np.array([40.0, 40.0]), np.array([-100.0, -99.0])  # ~53 mi
        dlat, dlon = geo.densify(lat, lon, max_step_miles=0.5)
        steps = geo.haversine_miles(dlat[:-1], dlon[:-1], dlat[1:], dlon[1:])
        self.assertLessEqual(steps.max(), 0.5 + 1e-6)
        self.assertEqual((dlat[0], dlon[0], dlat[-1], dlon[-1]), (40.0, -100.0, 40.0, -99.0))

    def test_chord_round_trip(self):
        self.assertAlmostEqual(float(geo.chord_to_miles(geo.miles_to_chord(12.5))), 12.5)

    def test_thin_polyline(self):
        coords = [[i, 0] for i in range(10_000)]
        out = geo.thin_polyline(coords, 100)
        self.assertLessEqual(len(out), 100)
        self.assertEqual(out[0], coords[0])
        self.assertEqual(out[-1], coords[-1])


def make_station(opis_id, lat, lon, price, **kw):
    return FuelStation.objects.create(
        opis_id=opis_id, name=f"S{opis_id}", address="I-80", city="X", state="NE",
        price=price, latitude=lat, longitude=lon, geocode_source="test", **kw,
    )


class StationsAlongRouteTests(TestCase):
    def setUp(self):
        stations.reset_table()
        # Route runs due east along lat 41 from lon -100 to -98 (~104 mi).
        make_station(1, 41.00, -99.5, "3.10")  # on the road, ~26 mi in
        make_station(2, 41.10, -99.0, "3.00")  # ~7 mi north, ~52 mi in
        make_station(3, 41.50, -98.5, "2.50")  # ~35 mi off route
        make_station(4, 41.00, -95.0, "2.00")  # well past the end

    def tearDown(self):
        stations.reset_table()

    def test_finds_nearby_stations_in_route_order(self):
        lat, lon = np.array([41.0, 41.0]), np.array([-100.0, -98.0])
        found = stations.stations_along_route(lat, lon, max_detour_miles=10)
        self.assertEqual([s.opis_id for s in found], [1, 2])
        self.assertAlmostEqual(found[0].mile, 26, delta=1)
        self.assertAlmostEqual(found[0].off_route_miles, 0, delta=0.5)
        self.assertAlmostEqual(found[1].off_route_miles, 6.9, delta=0.5)

    def test_wider_corridor_includes_more(self):
        lat, lon = np.array([41.0, 41.0]), np.array([-100.0, -98.0])
        found = stations.stations_along_route(lat, lon, max_detour_miles=40)
        self.assertEqual({s.opis_id for s in found}, {1, 2, 3})
