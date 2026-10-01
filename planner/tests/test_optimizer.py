from django.test import SimpleTestCase

from planner.services.optimizer import (
    Candidate,
    InfeasibleRoute,
    plan_fuel_stops,
    prune_dominated,
)


def c(mile, price, key=None):
    return Candidate(key=key or f"s{mile}", mile=mile, price=price)


class PlanFuelStopsTests(SimpleTestCase):
    def test_short_trip_needs_no_fuel_on_full_tank(self):
        plan = plan_fuel_stops([c(100, 3.0)], total_miles=400)
        self.assertEqual(plan.purchases, [])
        self.assertEqual(plan.total_cost, 0)
        self.assertAlmostEqual(plan.gallons_used, 40)

    def test_buys_only_enough_to_finish(self):
        # 700 mi: full tank gets to 500; must buy at the only station.
        plan = plan_fuel_stops([c(400, 3.0)], total_miles=700)
        (p,) = plan.purchases
        self.assertAlmostEqual(p.fuel_on_arrival, 10)  # 50 - 40
        self.assertAlmostEqual(p.gallons, 20)  # needs 30 for last 300 mi
        self.assertAlmostEqual(plan.total_cost, 60)
        self.assertAlmostEqual(plan.fuel_at_destination, 0)

    def test_fills_up_at_cheap_station_before_expensive_stretch(self):
        stops = [c(100, 2.0), c(450, 4.0)]
        plan = plan_fuel_stops(stops, total_miles=900)
        by_key = {p.key: p for p in plan.purchases}
        # Cheap station: top up the 10 gal used getting there.
        self.assertAlmostEqual(by_key["s100"].gallons, 10)
        # Expensive one: only what's needed for the last 450 mi.
        self.assertAlmostEqual(by_key["s450"].gallons, 30)
        self.assertAlmostEqual(plan.total_cost, 10 * 2 + 30 * 4)

    def test_buys_just_enough_to_reach_cheaper_station(self):
        stops = [c(450, 4.0), c(600, 2.0)]
        plan = plan_fuel_stops(stops, total_miles=1000)
        by_key = {p.key: p for p in plan.purchases}
        # At 450 we have 5 gal; need 15 to reach the $2 station.
        self.assertAlmostEqual(by_key["s450"].gallons, 10)
        # At 600, empty; 400 mi to go = 40 gal, all at $2.
        self.assertAlmostEqual(by_key["s600"].gallons, 40)
        self.assertAlmostEqual(plan.total_cost, 10 * 4 + 40 * 2)

    def test_matches_brute_force_on_small_cases(self):
        """Compare against exhaustive search over which stations to fill/visit."""
        stops = [c(150, 3.1), c(320, 2.7), c(480, 3.5), c(610, 2.9), c(900, 3.0), c(1150, 2.5)]
        total = 1400
        plan = plan_fuel_stops(stops, total)

        # Brute force at 1-gallon granularity: DP over (station, fuel) states.
        cap = 50
        nodes = [(0, None)] + [(s.mile, s.price) for s in stops] + [(total, 0.0)]
        best = {(0, 50): 0.0}
        for i in range(1, len(nodes)):
            new = {}
            for (j, f), cost in best.items():
                for k in range(j + 1, len(nodes)):
                    d = (nodes[k][0] - nodes[j][0]) / 10
                    if d > cap:
                        break
                    price = nodes[j][1]
                    for leave in range(f, cap + 1) if price is not None else [f]:
                        if leave < d:
                            continue
                        arr = leave - d
                        new_cost = cost + (leave - f) * (price or 0)
                        key = (k, round(arr))
                        if abs(arr - round(arr)) > 1e-9:
                            continue
                        if new_cost < new.get(key, float("inf")):
                            new[key] = new_cost
            best.update({k: min(v, best.get(k, float("inf"))) for k, v in new.items()})
        optimum = min(v for (k, _), v in best.items() if k == len(nodes) - 1)
        self.assertAlmostEqual(plan.total_cost, optimum, places=6)

    def test_gap_longer_than_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRoute):
            plan_fuel_stops([c(400, 3.0)], total_miles=1000)

    def test_empty_start_tank_reports_reach(self):
        with self.assertRaisesRegex(InfeasibleRoute, "Starting fuel only covers 0 mi"):
            plan_fuel_stops([c(20, 3.0)], total_miles=600, start_fuel_gallons=0)

    def test_partial_start_tank(self):
        plan = plan_fuel_stops([c(20, 3.0)], total_miles=300, start_fuel_gallons=5)
        self.assertAlmostEqual(plan.gallons_purchased, 25)  # 300 mi = 30 gal, had 5

    def test_min_purchase_skips_top_offs(self):
        # Pure greedy would top off 0.8 gal at mile 108 after filling at 100.
        stops = [c(100, 2.0), c(108, 2.1), c(450, 3.0)]
        greedy = plan_fuel_stops(stops, total_miles=900)
        self.assertIn("s108", [p.key for p in greedy.purchases])

        plan = plan_fuel_stops(stops, total_miles=900, min_purchase_gallons=10)
        self.assertEqual([p.key for p in plan.purchases], ["s100", "s450"])
        self.assertAlmostEqual(plan.gallons_purchased, 40)

    def test_min_purchase_never_breaks_feasibility(self):
        # At mile 495 we must buy even though the amount is small.
        stops = [c(495, 3.0), c(800, 3.0)]
        plan = plan_fuel_stops(stops, total_miles=900, min_purchase_gallons=10)
        self.assertGreater(len(plan.purchases), 0)

    def test_forced_stop_buys_at_least_minimum(self):
        # Pure greedy limps 466 -> 524 -> 563 buying ~2.5 and ~3.9 gal; a forced
        # stop should buy enough to skip the middle one.
        stops = [c(466, 3.5), c(524, 3.38), c(563, 3.0)]
        greedy = plan_fuel_stops(stops, total_miles=1000)
        self.assertEqual([p.key for p in greedy.purchases], ["s466", "s524", "s563"])

        plan = plan_fuel_stops(stops, total_miles=1000, min_purchase_gallons=10)
        self.assertEqual([p.key for p in plan.purchases], ["s466", "s563"])
        self.assertAlmostEqual(plan.purchases[0].gallons, 10)
        self.assertAlmostEqual(plan.fuel_at_destination, 0)

    def test_forced_stop_near_destination_buys_only_what_is_needed(self):
        plan = plan_fuel_stops([c(495, 3.0)], total_miles=520, min_purchase_gallons=10)
        self.assertAlmostEqual(plan.gallons_purchased, 2.0)
        self.assertAlmostEqual(plan.fuel_at_destination, 0)


class PruneDominatedTests(SimpleTestCase):
    def test_keeps_only_cheapest_in_cluster(self):
        stops = [c(100, 3.4), c(103, 3.2), c(110, 3.5), c(300, 3.6)]
        kept = [s.mile for s in prune_dominated(stops, 25)]
        self.assertEqual(kept, [103, 300])

    def test_equal_prices_keep_first(self):
        stops = [c(100, 3.0), c(100, 3.0, key="dup")]
        self.assertEqual(len(prune_dominated(stops, 25)), 1)

    def test_zero_window_is_noop(self):
        stops = [c(100, 3.4), c(103, 3.2)]
        self.assertEqual(prune_dominated(stops, 0), stops)
