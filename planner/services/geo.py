"""Vectorised geometry helpers. Coordinates are (lat, lon) in degrees unless noted."""

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance; works on scalars or equal-shaped arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def to_unit_vectors(lat, lon):
    """Lat/lon -> 3D points on the unit sphere, so a KD-tree can do nearest-neighbour
    search with straight-line (chord) distance, which ranks the same as great-circle."""
    lat, lon = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def miles_to_chord(miles):
    return 2 * np.sin(np.asarray(miles) / (2 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2, 0.0, 1.0))


def densify(lat, lon, max_step_miles=0.5):
    """Insert points so no gap along the polyline exceeds max_step_miles.

    Route vertices can be miles apart on straight interstates, which would make
    "nearest route point" a poor measure of how far a station is from the road.
    Linear interpolation in lat/lon is fine at sub-mile steps.
    """
    seg = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    pieces = np.maximum(1, np.ceil(seg / max_step_miles).astype(int))
    out_lat, out_lon = [], []
    for i, n in enumerate(pieces):
        t = np.arange(n) / n
        out_lat.append(lat[i] + (lat[i + 1] - lat[i]) * t)
        out_lon.append(lon[i] + (lon[i + 1] - lon[i]) * t)
    out_lat.append(lat[-1:])
    out_lon.append(lon[-1:])
    return np.concatenate(out_lat), np.concatenate(out_lon)


def cumulative_miles(lat, lon):
    seg = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    return np.concatenate(([0.0], np.cumsum(seg)))


def thin_polyline(coords, max_points):
    """Keep roughly evenly spaced vertices (by index) for display, always keeping
    both endpoints. `coords` is a list of [lon, lat] pairs."""
    if len(coords) <= max_points:
        return coords
    idx = np.unique(np.linspace(0, len(coords) - 1, max_points).round().astype(int))
    return [coords[i] for i in idx]
