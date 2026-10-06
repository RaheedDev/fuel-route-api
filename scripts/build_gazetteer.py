"""
Build the small offline geo datasets bundled in ``data/`` (run once, by hand).

Why: the API must not call an online geocoder for every station (~6,700 of
them) or for every request. Instead we ship three tiny public-domain files
derived from the US Census Bureau and look things up locally:

* ``data/us_places.csv.gz`` - city/town/CDP centroids (Census Gazetteer "places",
  plus "county subdivisions" = New England/Midwest towns & townships, plus
  USGS GNIS "populated places" for small unincorporated communities that
  truck stops are often listed under, e.g. "Ruther Glen, VA").
* ``data/us_zips.csv.gz`` - ZIP code (ZCTA) centroids.
* ``data/us_border.json.gz`` - US outline (Census cartographic boundary, 1:5M)
  used to reject coordinates outside the USA.

Usage (inputs from https://www2.census.gov/geo/ and the USGS GNIS
"DomesticNames_National_Text.zip" on prd-tnm.s3.amazonaws.com):

    pip install pyshp
    python scripts/build_gazetteer.py \
        --places 2025_Gaz_place_national.txt \
        --cousubs 2025_Gaz_cousubs_national.txt \
        --gnis DomesticNames_National.txt \
        --zctas 2025_Gaz_zcta_national.txt \
        --nation cb_2024_us_nation_5m.shp
"""

import argparse
import csv
import gzip
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))

# Same key function the app uses at runtime, so build-time de-duplication
# matches runtime lookups exactly.
from planner.services.gazetteer import US_STATES, normalize_place  # noqa: E402

STATE_CODE_BY_NAME = {name: code for code, name in US_STATES.items()}

# Census appends a legal/statistical descriptor to names ("Dallas city",
# "Abanda CDP", "Waltham city", "Ann Arbor charter township", ...). We strip
# it so the name matches what people (and the fuel CSV) write.
DESCRIPTOR_RE = re.compile(
    r"\s+(city and borough|consolidated government|unified government|"
    r"metropolitan government|metro government|urban county|charter township|"
    r"city|town|village|borough|CDP|municipality|township|plantation|"
    r"gore|grant|location|purchase|corporation)"
    r"(\s*\(balance\))?$"
)

# County subdivisions that are NOT real towns (statistical divisions whose
# centroid can be far from the town they are named after).
SKIP_COUSUB_RE = re.compile(
    r"(CCD|precinct|district|barrio|comunidad|zona urbana|census subarea|"
    r"not defined|UT|unorganized territory)$",
    re.IGNORECASE,
)


def clean_name(raw):
    name = DESCRIPTOR_RE.sub("", raw.strip())
    return name.replace(" (balance)", "").strip()


def read_gazetteer(path):
    with open(path, encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="|")
        header = [h.strip() for h in next(reader)]
        for row in reader:
            yield dict(zip(header, (v.strip() for v in row)))


def read_gnis_populated_places(gnis_path):
    """{(STATE, key): [(name, lat, lng), ...]} for every GNIS populated place."""
    places = defaultdict(list)
    with open(gnis_path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="|"):
            if row["feature_class"] != "Populated Place":
                continue
            state = STATE_CODE_BY_NAME.get(row["state_name"])
            if not state or not row["prim_lat_dec"] or float(row["prim_lat_dec"]) == 0:
                continue
            key = (state, normalize_place(row["feature_name"]))
            places[key].append((row["feature_name"], float(row["prim_lat_dec"]),
                                float(row["prim_long_dec"])))
    return places


def approx_miles(lat1, lng1, lat2, lng2):
    return 69 * math.hypot(lat2 - lat1, (lng2 - lng1) * math.cos(math.radians(lat1)))


def unambiguous(hits):
    """The first hit, if all hits with this name are within ~25 miles of each other."""
    _, lat0, lng0 = hits[0]
    if all(approx_miles(lat0, lng0, lat, lng) <= 25 for _, lat, lng in hits):
        return hits[0]
    return None


def build_places(places_path, cousubs_path, gnis_path):
    gnis = read_gnis_populated_places(gnis_path)
    out = []

    def add_census(row, kind):
        state, name = row["USPS"], clean_name(row["NAME"])
        lat, lng = float(row["INTPTLAT"]), float(row["INTPTLONG"])
        # Census "internal points" can sit far from the town centre (San
        # Francisco's is out at sea because the city includes ocean). GNIS
        # marks the actual town centre, so prefer it when it clearly refers
        # to the same place.
        hit = unambiguous(gnis.get((state, normalize_place(name)), [(None, lat, lng)]))
        if hit and approx_miles(lat, lng, hit[1], hit[2]) <= 50:
            lat, lng = hit[1], hit[2]
        out.append((state, name, lat, lng, kind))

    for row in read_gazetteer(places_path):
        add_census(row, "place")
    for row in read_gazetteer(cousubs_path):
        if not SKIP_COUSUB_RE.search(row["NAME"]):
            add_census(row, "cousub")

    # Small unincorporated communities that only GNIS knows about.
    known = {(state, normalize_place(name)) for state, name, *_ in out}
    for (state, key), hits in gnis.items():
        hit = unambiguous(hits)
        if (state, key) not in known and hit:
            out.append((state, hit[0], hit[1], hit[2], "gnis"))

    # Row order = match priority (see planner.services.gazetteer._places_index).
    with gzip.open(DATA_DIR / "us_places.csv.gz", "wt", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["state", "name", "lat", "lng", "kind"])
        for state, name, lat, lng, kind in out:
            writer.writerow([state, name, f"{lat:.4f}", f"{lng:.4f}", kind])
    print(f"us_places.csv.gz: {len(out)} rows")


def build_zips(zcta_path):
    rows = list(read_gazetteer(zcta_path))
    with gzip.open(DATA_DIR / "us_zips.csv.gz", "wt", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["zip", "lat", "lng"])
        for row in rows:
            writer.writerow([row["GEOID"], f"{float(row['INTPTLAT']):.4f}",
                             f"{float(row['INTPTLONG']):.4f}"])
    print(f"us_zips.csv.gz: {len(rows)} rows")


def build_border(nation_shp):
    import shapefile  # pyshp, only needed for this one-off build step

    shape = shapefile.Reader(nation_shp).shape(0)
    starts = list(shape.parts) + [len(shape.points)]
    rings = []
    for a, b in zip(starts[:-1], starts[1:]):
        ring = [[round(x, 4), round(y, 4)] for x, y in shape.points[a:b]]
        if len(ring) >= 4:
            rings.append(ring)
    # Outer rings and holes are stored alike; the even-odd point-in-polygon
    # test in planner.services.gazetteer handles holes automatically.
    with gzip.open(DATA_DIR / "us_border.json.gz", "wt") as fh:
        json.dump({"source": "US Census cb_2024_us_nation_5m", "rings": rings}, fh,
                  separators=(",", ":"))
    print(f"us_border.json.gz: {len(rings)} rings, {sum(map(len, rings))} points")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--places", required=True)
    parser.add_argument("--cousubs", required=True)
    parser.add_argument("--gnis", required=True)
    parser.add_argument("--zctas", required=True)
    parser.add_argument("--nation", required=True)
    args = parser.parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    build_places(args.places, args.cousubs, args.gnis)
    build_zips(args.zctas)
    build_border(args.nation)
