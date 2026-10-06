"""
Offline US place lookup ("gazetteer") built from bundled Census data.

Used in two places:
* ``load_fuel_stations`` - to give each truck stop a coordinate from its
  City + State (the fuel CSV has no lat/lng), without any API calls.
* request geocoding - to turn "Chicago, IL" or "60601" into a coordinate
  instantly, again without any API calls.

The data files are loaded lazily, once per process, and kept in memory
(~60k places + ~34k ZIPs, a few MB).
"""

import csv
import gzip
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from django.conf import settings

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}

# Abbreviations people (and the fuel CSV) use in place names.
_ANYWHERE_ABBREVIATIONS = {"st": "saint", "ste": "sainte"}  # "East St Louis"
_LEADING_ABBREVIATIONS = {  # "Ft Worth", "N Little Rock"
    "ft": "fort", "mt": "mount", "pt": "point",
    "n": "north", "s": "south", "e": "east", "w": "west",
}
_LEADING_WORDS = re.compile(r"^(town|city|village) of ")  # "Town of Pecos"
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize_place(name):
    """
    Turn a place name into a lookup key: 'St. Louis' -> 'saintlouis'.

    Spaces are removed at the end so spelling variants such as
    'Mc Calla' / 'McCalla' or 'De Forest' / 'DeForest' match.
    """
    # "Cañon City" -> "Canon City"
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ").replace("-", " ").replace("/", " ")
    text = _LEADING_WORDS.sub("", _NON_ALNUM.sub("", text).strip())
    words = [_ANYWHERE_ABBREVIATIONS.get(w, w) for w in text.split()]
    if words and words[0] in _LEADING_ABBREVIATIONS:
        words[0] = _LEADING_ABBREVIATIONS[words[0]]
    return "".join(words)


def normalize_state(value):
    """'tx' / 'Texas' -> 'TX'; returns None if it isn't a US state."""
    value = value.strip().rstrip(".")
    if value.upper() in US_STATES:
        return value.upper()
    return STATE_BY_NAME.get(value.lower())


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lng: float


def _aliases(name):
    """Extra, lower-priority names for a place."""
    # Consolidated cities: "Nashville-Davidson", "Louisville/Jefferson County".
    first = re.split(r"[-/]", name)[0].strip()
    if first != name:
        yield first
    # "Boise City" (official) is usually written "Boise".
    if name.endswith(" City"):
        yield name[: -len(" City")]


@lru_cache(maxsize=1)
def _places_index():
    """
    {(STATE, key): Place}.

    The CSV is ordered by quality (Census incorporated places/CDPs, then
    towns/townships, then USGS GNIS communities), so ``setdefault`` keeps the
    best match when a name appears more than once.
    """
    index = {}
    aliases = {}
    with gzip.open(settings.GEO_DATA_DIR / "us_places.csv.gz", "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            place = Place(row["name"], row["state"], float(row["lat"]), float(row["lng"]))
            index.setdefault((place.state, normalize_place(place.name)), place)
            for alias in _aliases(place.name):
                aliases.setdefault((place.state, normalize_place(alias)), place)
    for key, place in aliases.items():
        index.setdefault(key, place)
    return index


@lru_cache(maxsize=1)
def _zip_index():
    with gzip.open(settings.GEO_DATA_DIR / "us_zips.csv.gz", "rt", encoding="utf-8") as fh:
        return {row["zip"]: (float(row["lat"]), float(row["lng"])) for row in csv.DictReader(fh)}


def find_place(city, state):
    """Look up a city/town centre point. Returns a Place or None."""
    state_code = normalize_state(state or "")
    if not state_code or not city:
        return None
    return _places_index().get((state_code, normalize_place(city)))


def find_zip(zip_code):
    """ZIP (5 digits) -> (lat, lng) or None."""
    return _zip_index().get(zip_code[:5])


@lru_cache(maxsize=1)
def _border_edges():
    """All edges of the US outline as numpy arrays (x1, y1, x2, y2)."""
    with gzip.open(settings.GEO_DATA_DIR / "us_border.json.gz", "rt") as fh:
        rings = json.load(fh)["rings"]
    edges = []
    for ring in rings:
        pts = np.asarray(ring, dtype=float)
        edges.append(np.hstack([pts, np.roll(pts, -1, axis=0)]))
    return np.vstack(edges).T


def is_in_usa(lat, lng):
    """
    True if the point is inside the US outline (Census 1:5M, incl. AK & HI).

    Vectorised ray casting: count how many outline edges a ray going east
    from the point crosses; an odd count means inside (holes work too).
    """
    x1, y1, x2, y2 = _border_edges()
    straddles = (y1 > lat) != (y2 > lat)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
    return bool(np.count_nonzero(straddles & (lng < x_cross)) % 2)
