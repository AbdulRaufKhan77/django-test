"""In-memory station table and the "which stations are along this route" query."""

import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from planner.models import FuelStation

from . import geo


@dataclass(frozen=True)
class StationTable:
    """All stations as parallel arrays, loaded once per process (~6.6k rows)."""

    ids: np.ndarray
    prices: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    xyz: np.ndarray
    records: dict  # opis_id -> dict of display fields


@dataclass(frozen=True)
class RouteStation:
    opis_id: int
    price: float
    mile: float  # distance along the route from the start
    off_route_miles: float


_table: StationTable | None = None
_lock = threading.Lock()


def get_table() -> StationTable:
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                _table = _load()
    return _table


def reset_table() -> None:
    """Drop the cached table (tests, or after re-importing stations)."""
    global _table
    _table = None


def _load() -> StationTable:
    rows = list(
        FuelStation.objects.values(
            "opis_id", "name", "address", "city", "state", "price", "latitude", "longitude"
        )
    )
    lat = np.array([r["latitude"] for r in rows], dtype=float)
    lon = np.array([r["longitude"] for r in rows], dtype=float)
    return StationTable(
        ids=np.array([r["opis_id"] for r in rows], dtype=np.int64),
        prices=np.array([float(r["price"]) for r in rows], dtype=float),
        lat=lat,
        lon=lon,
        xyz=geo.to_unit_vectors(lat, lon) if rows else np.empty((0, 3)),
        records={r["opis_id"]: r for r in rows},
    )


def stations_along_route(route_lat, route_lon, max_detour_miles) -> list[RouteStation]:
    """Return stations within `max_detour_miles` of the route, ordered by how far
    along the route they sit.

    The route is densified to half-mile spacing and put in a KD-tree; every
    station then asks for its nearest route point. That gives both the off-route
    distance and the station's mile marker (the cumulative distance at that point)
    in one vectorised query.
    """
    table = get_table()
    if len(table.ids) == 0:
        return []

    dlat, dlon = geo.densify(np.asarray(route_lat), np.asarray(route_lon))
    miles = geo.cumulative_miles(dlat, dlon)

    # Cheap prefilter: skip stations outside the route's bounding box (+ margin).
    margin_lat = max_detour_miles / 69.0 + 0.01
    margin_lon = margin_lat / max(np.cos(np.radians(np.abs(dlat).max())), 0.1)
    in_box = (
        (table.lat >= dlat.min() - margin_lat)
        & (table.lat <= dlat.max() + margin_lat)
        & (table.lon >= dlon.min() - margin_lon)
        & (table.lon <= dlon.max() + margin_lon)
    )
    candidates = np.flatnonzero(in_box)
    if candidates.size == 0:
        return []

    tree = cKDTree(geo.to_unit_vectors(dlat, dlon))
    chord, nearest = tree.query(
        table.xyz[candidates], distance_upper_bound=geo.miles_to_chord(max_detour_miles)
    )
    hit = np.isfinite(chord)
    found = candidates[hit]
    off = geo.chord_to_miles(chord[hit])
    mile = miles[nearest[hit]]

    order = np.lexsort((table.prices[found], mile))
    return [
        RouteStation(
            opis_id=int(table.ids[found[k]]),
            price=float(table.prices[found[k]]),
            mile=float(mile[k]),
            off_route_miles=float(off[k]),
        )
        for k in order
    ]
