"""
Small, vectorised geometry helpers (numpy) for working with route polylines.

Coordinates are always (lat, lng) in degrees; distances are in miles.
"""

import numpy as np

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1, lng1, lat2, lng2):
    """Great-circle distance in miles. Works on scalars or numpy arrays."""
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2)
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def cumulative_miles(coords):
    """Distance from the start of the polyline to every vertex (array of len N)."""
    seg = haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    return np.concatenate(([0.0], np.cumsum(seg)))


def densify(coords, max_step_miles):
    """
    Insert points so that no segment is longer than ``max_step_miles``.

    OSRM polylines can have vertices miles apart on straight interstates;
    densifying lets a nearest-*vertex* search approximate the nearest point
    on the *line* to within ``max_step_miles / 2``.
    """
    seg = haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    pieces = np.maximum(1, np.ceil(seg / max_step_miles).astype(int))
    if pieces.max() == 1:
        return coords
    # For segment i we emit pieces[i] points at fractions 0, 1/k, ..., (k-1)/k.
    seg_index = np.repeat(np.arange(len(seg)), pieces)
    offsets = np.arange(len(seg_index)) - np.repeat(np.cumsum(pieces) - pieces, pieces)
    frac = (offsets / pieces[seg_index])[:, None]
    points = coords[seg_index] + frac * (coords[seg_index + 1] - coords[seg_index])
    return np.vstack([points, coords[-1:]])


def to_unit_xyz(lat, lng):
    """Lat/lng -> 3D points on the unit sphere (so a KD-tree can use them)."""
    lat, lng = np.radians(lat), np.radians(lng)
    return np.column_stack((np.cos(lat) * np.cos(lng), np.cos(lat) * np.sin(lng), np.sin(lat)))


def miles_to_chord(miles):
    """Surface distance -> straight-line distance between unit-sphere points."""
    return 2 * np.sin(np.asarray(miles) / (2 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2, 0, 1))


def decode_polyline(encoded, precision=5):
    """
    Decode a Google "encoded polyline" string (the format OSRM returns) into
    an (N, 2) array of (lat, lng). It is ~3x smaller over the wire than GeoJSON.
    """
    coords = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lng in (False, True):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lng:
                lng += delta
            else:
                lat += delta
        coords.append((lat, lng))
    return np.array(coords, dtype=float) / 10**precision


def downsample(coords, miles, step_miles):
    """
    Keep roughly one vertex every ``step_miles`` (plus the end points).
    Used to thin OSRM's very dense polyline before the KD-tree search and to
    keep the returned GeoJSON small.
    """
    if len(coords) < 3:
        return coords
    marks = np.arange(0, miles[-1], step_miles)
    keep = np.unique(np.concatenate(([0], np.searchsorted(miles, marks), [len(coords) - 1])))
    keep = keep[keep < len(coords)]
    return coords[keep]
