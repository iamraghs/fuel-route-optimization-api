# Fuel Route Optimization API

An API that finds fuel-efficient routes between two US locations by optimizing where and how much to refuel along the way. It takes two locations, figures out the best route options, decides which fuel stations to stop at and how much fuel to buy at each one, all while trying to minimize the total fuel cost for the trip.

Built with Django, DRF, PostgreSQL with PostGIS for spatial queries, and Redis for caching. Uses Google Maps APIs for directions and geocoding.

---

## What This Project Does

The problem is straightforward: a truck needs to go from one city to another. It starts with a full tank. Along the way there are fuel stations with different prices. The API figures out:

- Which fuel stations to stop at
- How much fuel to buy at each stop
- Which route alternative gives the lowest total fuel cost

It handles everything from a short 50-mile trip (where you don't need to stop at all) to a cross-country 3000-mile run with multiple stops.

---

## API Reference

### POST /route/fuel-optimization/

Takes a start and finish location. Both can be either an address string or a lat/lng coordinate pair.

**Request with addresses:**

```json
{"start": "Los Angeles, CA", "finish": "New York, NY"}
```

**Request with coordinates:**

```json
{"start": {"lat": 34.0522, "lng": -118.2437}, "finish": {"lat": 40.7128, "lng": -74.0060}}
```

**Response:**

```json
{
  "status": "success",
  "request_id": "a1b2c3d4",
  "selected_route": {
    "route_id": "route_b",
    "distance_miles": 1480.5,
    "is_optimal": true,
    "reason": "Lowest total fuel cost",
    "estimated_total_fuel_consumption_gallons": 148.0,
    "estimated_total_fuel_cost": 510.42,
    "fuel_stops_required": 2,
    "route_map_link": "https://www.google.com/maps/dir/..."
  },
  "fuel_stops": [
    {
      "stop_number": 1,
      "station_name": "Love's Travel Stop",
      "city": "Oklahoma City",
      "state": "OK",
      "mile_marker": 430.2,
      "fuel_price_per_gallon": 3.449,
      "gallons_to_buy": 22.5,
      "fuel_cost": 77.60,
      "detour_miles": 0.8
    }
  ],
  "route_comparison": [
    {
      "route_id": "route_a",
      "distance_miles": 1400.0,
      "estimated_total_fuel_cost": 620.00,
      "fuel_stops_required": 3,
      "selected": false
    },
    {
      "route_id": "route_b",
      "distance_miles": 1480.5,
      "estimated_total_fuel_cost": 510.42,
      "fuel_stops_required": 2,
      "selected": true
    }
  ],
  "trip_summary": {
    "total_distance_miles": 1480.5,
    "total_fuel_consumed_gallons": 148.0,
    "total_fuel_cost": 510.42,
    "average_price_per_gallon": 3.45,
    "total_fuel_stops": 2,
    "starting_fuel_gallons": 50,
    "fuel_purchased_at_stops": 98.0,
    "total_fuel_available": 148.0,
    "fuel_remaining_at_destination": 0.0
  }
}
```

The response includes the selected route with its fuel stops, comparison data for all route alternatives, and a trip summary with full fuel accounting.

---

## How It Works

### Request Flow

When a request comes in, the first thing that happens is a cache check. If someone has already asked for the same route with the same fuel prices, the cached response comes back immediately without any API calls or computation.

If its a cache miss, the system geocodes the start and end locations in parallel (two threads). Once it has coordinates, it calls the Google Directions API to get up to two route alternatives.

For routes under 500 miles, the API takes a fast path. A full tank covers that distance, so theres no need to search for fuel stations at all. The response comes back with zero fuel stops and an estimated cost based on average fuel price.

For longer routes, the system queries the fuel station database to find stations near each route. It uses PostGIS spatial queries to find stations within a corridor along the route. This is much faster than loading all stations and filtering in Python.

Each route alternative goes through the optimization engine independently. The optimizer figures out the best fuel stops, then the route comparator picks the one with the lowest total fuel cost.

The result gets cached in Redis so the next request for the same route returns instantly.

### Fuel Optimization Strategy

The optimizer uses what I call a range-aware greedy approach. It starts at position zero with a full tank and works its way toward the destination.

At each decision point (which is every time it considers a stop), it figures out how far it can actually go based on current fuel level and the reserve requirement. Only stations within that reachable range are considered as candidates.

The decision logic works like this:

If the destination is reachable from a candidate station after filling up, and theres no cheaper station ahead that we can reach directly, then this is the last stop. Buy only what you need to get to the destination, nothing extra.

If theres a cheaper station within range from the candidate station, but we cant reach it directly from where we are now, buy just enough fuel at this station to make it to that cheaper station.

If theres no cheaper station ahead within the range we would have after filling up, fill the tank. This maximizes flexibility for whatever comes next.

The algorithm recomputes everything at every stop. It doesnt commit to a multi-stop plan and then follow it blindly. After each stop, it re-evaluates using the current fuel level and position. This is important because new stations become reachable as you move forward.

### Safety Guards

A few things keep the optimization practical:

- Minimum 5-gallon purchase prevents micro-stops that waste time
- Stops too close together (under 80 miles with plenty of fuel) get skipped
- A 50-mile reserve ensures you never run dry
- Stations more than 5 miles off the route are excluded
- If skipping a station would leave no other options, the algorithm fills up anyway to keep moving

### Caching

There are several layers of caching because the costliest part of this system is the Google API calls.

The optimization cache stores the full API response in Redis for an hour. The cache key includes the price version ID, so when fuel prices update, old cached responses naturally become different keys and stop being served.

Route geometry from Google Directions gets cached in PostgreSQL for 24 hours. Geocoding results go to Redis for 7 days. Station corridor filters (which stations are near which route) get cached in Redis for an hour.

There is also an in-process cache for decoded polyline geometry that avoids repeated decoding of the same route data during a single request.

Request coalescing prevents duplicate Google API calls. If two people ask for the same route at the same time, only one request goes to Google. The other waits for the result.

### Station Query

For each route, the system needs to find fuel stations along the corridor. It starts with all stations that have active prices, then filters them using PostGIS spatial queries. The stations table has a GIST index on the location point, so this is a fast spatial lookup rather than a full table scan.

If PostGIS isnt available (like in test environments with SQLite), it falls back to a bounding box pre-filter followed by a haversine distance check against the route polyline.

---

## Project Structure

```
fuel_routing/
  api.py              - REST API endpoint
  views.py            - Health check endpoint
  engine.py           - Main orchestrator that ties everything together
  optimizer.py        - Fuel stop optimization algorithm
  cache_service.py    - Optimization result cache layer
  cache_utils.py      - Geometry cache, corridor cache, request locking
  routing.py          - Google Directions API integration
  geocoding.py        - Google Geocoding API integration
  stations.py         - Fuel station query and filtering
  route_selector.py   - Route comparison and selection
  route_geometry.py   - Route validation and stop sequencing
  constants.py        - Vehicle parameters and configuration
  models.py           - Database models
  serializers.py      - Request/response serializers
  preprocessing.py    - CSV data preprocessing pipeline
```

---

## Setup

```bash
# Create virtual environment and install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Database setup (PostgreSQL with PostGIS required)
export DATABASE_URL=postgis://user:password@localhost:5432/fuel_routing
python manage.py migrate

# Load fuel station data
python manage.py preprocess_fuel_data
python manage.py load_fuel_data fuel_prices_cleaned.csv

# Configure Google Maps API
export GOOGLE_MAPS_API_KEY=your_key_here

# Start server
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4
```

---

## Testing

The test suite sends requests to a running server and validates every field in the response. It covers 180 routes across short trips (under 500 miles), medium trips, long trips, and coast-to-coast routes.

```bash
# Start the server first, then run tests
python test_all.py
```

Each test checks that the response includes all required fields, that the fuel accounting adds up (starting fuel plus purchased fuel minus consumed fuel equals remaining fuel), that stop costs are calculated correctly (price times gallons equals cost), and that distances are consistent.

---

## Configuration

Key parameters that control the vehicle model and optimization behavior are in `constants.py`:

- Vehicle fuel efficiency (miles per gallon)
- Fuel tank capacity in gallons
- Reserve fuel buffer in miles
- Maximum detour distance in miles
- Minimum fuel purchase amount in gallons
- Corridor buffer width for station queries

These are set through Django settings and can be adjusted per deployment.

---

## Design Notes

A few decisions that came up during development:

The optimization algorithm uses a greedy approach rather than dynamic programming. The tank range naturally limits how far ahead you need to look, and recomputing at every stop catches cases where the greedy choice wasnt ideal. This keeps the computation fast while still producing good results.

The API always evaluates two route alternatives from Google Directions and picks the cheaper one. Three routes were considered but the third was usually a minor variation of the first two and added compute time without meaningful benefit.

Redis is used for most caching because sub-millisecond reads matter when the system is handling many requests. PostgreSQL RouteCache exists only for route geometry that benefits from PostGIS spatial types.

The station corridor filter uses PostGIS spatial queries with a GIST index. An earlier version did all filtering in Python with haversine calculations, but the spatial SQL approach is significantly faster with thousands of stations to evaluate.

Route and geocode inputs are normalized before computing cache keys. This means Los Angeles, CA and los angeles, ca produce the same key and share a cached result.
