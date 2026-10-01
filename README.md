# Fuel Route Planner

A Django REST API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON line, plus a link to a Leaflet map),
- the cheapest set of fuel stops for a vehicle with a **500-mile range**,
- the **total fuel cost** at **10 mpg**, using the prices in
  `data/fuel-prices-for-be-assessment.csv`.

A trip costs **one** routing call to [OpenRouteService](https://openrouteservice.org),
plus up to two geocoding calls when locations are given as text instead of
coordinates. All of these responses are cached, so a repeat trip makes no
external calls.

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add your free ORS_API_KEY
python manage.py migrate
python manage.py import_stations  # loads ~6.6k stations from the CSV
python manage.py runserver
```

## API

`GET /api/route/?start=Chicago, IL&finish=Denver, CO`

`POST /api/route/` with a JSON body:

```json
{"start": "New York, NY", "finish": {"lat": 34.05, "lon": -118.24}}
```

| Parameter            | Description                                                       |
|----------------------|-------------------------------------------------------------------|
| `start`, `finish`    | Place name/address, `"lat,lon"`, or `{"lat": .., "lon": ..}`       |
| `max_detour_miles`   | How far off the road a station may be (default 10, max 50)          |
| `start_fuel_gallons` | Fuel in the tank at the start (default: full tank, 50 gal)        |
| `format`             | `json` (default) or `geojson` (FeatureCollection of route + stops) |

The response contains `route` (distance, duration, geometry), `fuel_stops` (station,
price, mile marker, gallons bought, cost), `summary` (`total_fuel_cost`, gallons,
number of stops), `assumptions`, `meta` (routing calls made, timing) and `map_url`.

Errors: `400` invalid input, `422` the trip can't be planned (outside the USA, no
station within range), `502` the routing service failed.

Other endpoints: `GET /map/?start=..&finish=..` (interactive map),
`GET /api/health/` (station count).

## How it works

1. **Stations.** The price CSV has no coordinates, and its addresses are highway
   exits, so each station is placed at its city's coordinates.
   `data/city_coords.csv` was built once by `scripts/build_city_coords.py` from the
   Census Gazetteer and GeoNames, plus hand-checked overrides in
   `data/city_overrides.csv`. It is committed, so nothing is geocoded at request time.
2. **Route corridor.** The route polyline is densified, and a KD-tree over the
   station positions (as 3D unit vectors) finds every station within
   `max_detour_miles` of the road, along with its mile marker.
3. **Optimisation.** This is the classic gas-station problem. At each station: if a
   cheaper station (or the destination) is within one tank, buy just enough to
   reach it; otherwise fill up. For a fixed route this greedy rule is optimal. Two
   practical tweaks:
   - only the cheapest station in any 25-mile stretch is considered, and
   - purchases under 10 gal are skipped when the next station can still be reached.
     When a stop can't be skipped, at least 10 gal are bought so the driver
     doesn't stop again a few miles later.

   Both tweaks are in `planner/services/optimizer.py` and configured in
   `FUEL_PLANNER` in `config/settings.py`.

Assumptions: fuel already in the tank at the start is not counted in the cost, and
the extra distance to drive off the road to a station is ignored.

## Tests

```bash
python manage.py test planner
```

The tests stub the routing client, so no API key is needed. The optimizer is
checked against a brute-force search on small random cases.
