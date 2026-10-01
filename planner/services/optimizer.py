"""Cheapest refuelling plan along a fixed route (the classic "gas station problem").

At each station, in order along the route:

* If a cheaper station (or the destination) is reachable on a full tank, buy only
  enough fuel to get there.
* Otherwise this is the cheapest fuel for the next full tank's worth of road, so
  fill up.

This greedy rule is optimal for a fixed route and linear prices: each gallon is
bought at the cheapest station that could have supplied it. It runs in
O(n * k) for n stations and k stations within one tank's range.

Pure cost-optimality produces silly stops ("top off 1.3 gal, 12 miles after the
last fill-up"), so purchases smaller than `min_purchase_gallons` are skipped
whenever the tank can still reach the next station. Skipping a purchase never
makes the trip infeasible; it costs at most a few cents per skipped stop.
"""

from dataclasses import dataclass, field

EPS = 1e-9


class InfeasibleRoute(Exception):
    """A stretch of the route is longer than the vehicle's range with no station."""

    def __init__(self, from_mile: float, to_mile: float, range_miles: float, start_reach=None):
        self.from_mile, self.to_mile = from_mile, to_mile
        if start_reach is not None and start_reach < range_miles:
            message = (
                f"Starting fuel only covers {start_reach:.0f} mi but the first reachable "
                f"fuel stop is at mile {to_mile:.1f}."
            )
        else:
            message = (
                f"No fuel station within range between mile {from_mile:.0f} and "
                f"mile {to_mile:.0f} ({to_mile - from_mile:.0f} mi gap, range "
                f"{range_miles:.0f} mi)."
            )
        super().__init__(message)


@dataclass(frozen=True)
class Candidate:
    key: object  # opaque station identifier, passed back in the plan
    mile: float
    price: float


@dataclass(frozen=True)
class Purchase:
    key: object
    mile: float
    price: float
    gallons: float
    fuel_on_arrival: float

    @property
    def cost(self) -> float:
        return self.gallons * self.price


@dataclass
class FuelPlan:
    purchases: list[Purchase] = field(default_factory=list)
    gallons_used: float = 0.0
    start_fuel: float = 0.0
    fuel_at_destination: float = 0.0

    @property
    def total_cost(self) -> float:
        return sum(p.cost for p in self.purchases)

    @property
    def gallons_purchased(self) -> float:
        return sum(p.gallons for p in self.purchases)


def prune_dominated(candidates: list[Candidate], window_miles: float) -> list[Candidate]:
    """Drop stations that have a cheaper (or equally cheap, earlier) station within
    `window_miles` along the route.

    Interstate exits come in clusters, and the greedy rule would otherwise hop
    between them buying a gallon at each. Keeping only the local price minima
    gives a plan a driver would actually follow. `candidates` must be sorted by mile.
    """
    if window_miles <= 0:
        return list(candidates)
    kept, lo, hi, n = [], 0, 0, len(candidates)
    for i, c in enumerate(candidates):
        while candidates[lo].mile < c.mile - window_miles:
            lo += 1
        while hi + 1 < n and candidates[hi + 1].mile <= c.mile + window_miles:
            hi += 1
        dominated = any(
            o.price < c.price or (o.price == c.price and j < i)
            for j, o in enumerate(candidates[lo : hi + 1], start=lo)
            if j != i
        )
        if not dominated:
            kept.append(c)
    return kept


def plan_fuel_stops(
    candidates: list[Candidate],
    total_miles: float,
    range_miles: float = 500.0,
    mpg: float = 10.0,
    start_fuel_gallons: float | None = None,
    min_purchase_gallons: float = 0.0,
) -> FuelPlan:
    """Plan purchases for a trip of `total_miles`.

    `candidates` must be sorted by mile. `start_fuel_gallons` defaults to a full
    tank. Fuel already in the tank at the start is not counted as spent.
    """
    capacity = range_miles / mpg
    fuel = capacity if start_fuel_gallons is None else min(start_fuel_gallons, capacity)
    plan = FuelPlan(start_fuel=fuel, gallons_used=total_miles / mpg)

    # Stations past the destination are irrelevant; the destination acts as a
    # free "station" so the look-ahead naturally stops buying near the end.
    stops = [c for c in candidates if 0 <= c.mile <= total_miles]
    destination = Candidate(key=None, mile=total_miles, price=float("-inf"))
    nodes = stops + [destination]

    position = 0.0
    for i, node in enumerate(nodes):
        fuel -= (node.mile - position) / mpg
        if fuel < -EPS:
            start_reach = plan.start_fuel * mpg if i == 0 else None
            raise InfeasibleRoute(position, node.mile, range_miles, start_reach)
        position = node.mile
        if node is destination:
            break

        target_fuel = capacity  # assume fill-up unless something cheaper is in reach
        for ahead in nodes[i + 1 :]:
            if ahead.mile - node.mile > range_miles + EPS:
                break
            if ahead.price < node.price:
                target_fuel = (ahead.mile - node.mile) / mpg
                break

        buy = target_fuel - fuel
        if buy < min_purchase_gallons:
            next_stop = nodes[i + 1]
            if fuel * mpg + EPS >= next_stop.mile - node.mile:
                continue  # not worth stopping; we can still reach the next station
            # We have to stop anyway, so make it count: buy at least the minimum
            # (never more than fits, or than the rest of the trip needs) instead of
            # trickling in just enough to limp to the next stop.
            to_finish = (total_miles - node.mile) / mpg
            target_fuel = max(target_fuel, min(fuel + min_purchase_gallons, capacity, to_finish))
            buy = target_fuel - fuel
        if buy > 1e-6:
            plan.purchases.append(
                Purchase(
                    key=node.key,
                    mile=node.mile,
                    price=node.price,
                    gallons=buy,
                    fuel_on_arrival=max(fuel, 0.0),
                )
            )
            fuel = target_fuel

    plan.fuel_at_destination = max(fuel, 0.0)
    return plan
