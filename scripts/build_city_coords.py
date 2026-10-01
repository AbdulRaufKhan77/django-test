"""One-time builder for data/city_coords.csv.

The fuel price CSV has no coordinates and its "Address" column holds highway
exits ("I-44, EXIT 283 & US-69") that no geocoder resolves reliably. Stations
are therefore placed at the coordinates of their City + State, resolved in
this order:

0. data/city_overrides.csv, hand-checked coordinates for the few names the
   sources below miss or get wrong.
1. US Census Bureau Gazetteer, incorporated places and CDPs (public domain).
2. GeoNames US populated places (CC-BY 4.0), which covers the unincorporated
   communities and New England towns the Census places file leaves out.
3. Optionally (--ors), the OpenRouteService geocoder for anything left over.
   Check its answers before committing: for small towns it often returns a
   same-named place elsewhere in the state.

The output is committed to the repo, so this only needs re-running when the
price CSV changes. It never runs at request time.

    python scripts/build_city_coords.py [--ors]
"""

import argparse
import csv
import io
import os
import re
import sys
import zipfile
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PRICES_CSV = DATA_DIR / "fuel-prices-for-be-assessment.csv"
OUTPUT_CSV = DATA_DIR / "city_coords.csv"
OVERRIDES_CSV = DATA_DIR / "city_overrides.csv"  # hand-checked, wins over everything

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_place_national.zip"
)
GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
}

_LSAD_SUFFIX = re.compile(r"\s+(city|town|village|CDP|borough|municipality)$")
_GOVT_SUFFIX = re.compile(
    r"\s+(unified|consolidated|metropolitan|metro) government.*$|\s+urban county$"
)


def normalize(name: str) -> str:
    """Normalize a place name so 'Saint Johns', 'St. Johns' and 'St Johns' match."""
    s = name.strip().lower().replace("'", "").replace(".", "").replace("-", " ")
    s = re.sub(r"\b(sainte|saint|ste|st)\b", "st", s)
    s = re.sub(r"\bmc\s+", "mc", s)
    s = re.sub(r"\bde\s+", "de", s)
    s = re.sub(r"\bs\b", "south", s)
    return re.sub(r"\s+", " ", s).strip()


def gazetteer_name(raw: str) -> str:
    """'Sioux City city' -> 'Sioux City', 'Macon-Bibb County' -> 'Macon'."""
    name = re.sub(r"\s*\(balance\)$", "", raw)
    if _GOVT_SUFFIX.search(name) or "County" in name:
        name = _GOVT_SUFFIX.sub("", name)
        name = re.split(r"[-/]", name)[0]
    return _LSAD_SUFFIX.sub("", name)  # strip the LSAD word once, never twice


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"downloading {url}")
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


def load_gazetteer() -> dict:
    zpath = fetch(GAZETTEER_URL, RAW_DIR / "gazetteer_places.zip")
    with zipfile.ZipFile(zpath) as zf:
        text = zf.read(zf.namelist()[0]).decode("utf-8")
    index = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="|"):
        row = {k.strip(): v.strip() for k, v in row.items()}
        key = (row["USPS"], normalize(gazetteer_name(row["NAME"])))
        # Several places can share a name in one state; keep the largest by land area.
        area = float(row["ALAND"] or 0)
        if key not in index or area > index[key][2]:
            index[key] = (float(row["INTPTLAT"]), float(row["INTPTLONG"]), area)
    return {k: v[:2] for k, v in index.items()}


def load_geonames() -> dict:
    zpath = fetch(GEONAMES_URL, RAW_DIR / "geonames_US.zip")
    index = {}
    with zipfile.ZipFile(zpath) as zf, zf.open("US.txt") as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if f[6] != "P":  # feature class P = populated place
                continue
            state, pop = f[10], int(f[14] or 0)
            lat, lon = float(f[4]), float(f[5])
            for name in {f[1], f[2]}:
                key = (state, normalize(name.replace(" (historical)", "")))
                if key not in index or pop > index[key][2]:
                    index[key] = (lat, lon, pop)
    return {k: v[:2] for k, v in index.items()}


def ors_geocode(city: str, state: str, api_key: str):
    resp = requests.get(
        "https://api.openrouteservice.org/geocode/search",
        params={
            "api_key": api_key,
            "text": f"{city}, {state}",
            "boundary.country": "US",
            "layers": "locality,localadmin,neighbourhood,borough",
            "size": 1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    feats = resp.json().get("features", [])
    if feats and feats[0]["properties"].get("region_a") == state:
        lon, lat = feats[0]["geometry"]["coordinates"]
        return lat, lon
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ors", action="store_true", help="geocode leftovers via ORS")
    args = parser.parse_args()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    with PRICES_CSV.open(newline="") as fh:
        pairs = sorted(
            {
                (r["State"].strip(), r["City"].strip())
                for r in csv.DictReader(fh)
                if r["State"].strip() in US_STATES
            }
        )
    print(f"{len(pairs)} unique US city/state pairs")

    with OVERRIDES_CSV.open(newline="") as fh:
        overrides = {
            (r["state"], r["city"]): (float(r["lat"]), float(r["lon"]))
            for r in csv.DictReader(fh)
        }
    gazetteer, geonames = load_gazetteer(), load_geonames()
    api_key = os.environ.get("ORS_API_KEY", "")
    if args.ors and not api_key:
        try:
            from dotenv import dotenv_values

            api_key = dotenv_values(BASE_DIR / ".env").get("ORS_API_KEY", "")
        except ImportError:
            pass

    rows, missing = [], []
    for state, city in pairs:
        key = (state, normalize(city))
        if (state, city) in overrides:
            coords, source = overrides[(state, city)], "manual"
        elif key in gazetteer:
            coords, source = gazetteer[key], "census"
        elif key in geonames:
            coords, source = geonames[key], "geonames"
        elif args.ors and api_key and (hit := ors_geocode(city, state, api_key)):
            coords, source = hit, "ors"
        else:
            missing.append(f"{city}, {state}")
            continue
        rows.append((state, city, f"{coords[0]:.6f}", f"{coords[1]:.6f}", source))

    with OUTPUT_CSV.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["state", "city", "lat", "lon", "source"])
        writer.writerows(rows)

    counts = {}
    for r in rows:
        counts[r[4]] = counts.get(r[4], 0) + 1
    print(f"wrote {len(rows)} rows to {OUTPUT_CSV.relative_to(BASE_DIR)} {counts}")
    if missing:
        print(f"{len(missing)} unresolved: {'; '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
