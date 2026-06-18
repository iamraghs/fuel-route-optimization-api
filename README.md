# Fuel Route Optimization API

An API that computes minimum-fuel-cost routes between US locations by determining optimal refueling decisions across route alternatives. It handles everything from short trips (no stops needed) to coast-to-coast routes with multiple strategic refueling stops.

Built with Django, DRF, PostgreSQL with PostGIS for geospatial queries, Redis for caching, and Google Maps APIs for directions and geocoding.

---

## Table of Contents

- [Problem Statement](#problem-statement)
- [Core Features](#core-features)
- [Architecture Overview](#architecture-overview)
- [Request Lifecycle](#request-lifecycle)
- [Fuel Optimization Strategy](#fuel-optimization-strategy)
- [Route Selection](#route-selection)
- [Cache Architecture](#cache-architecture)
- [PostGIS Strategy](#postgis-strategy)
- [Edge Cases Handled](#edge-cases-handled)
- [Failure Handling](#failure-handling)
- [Performance Characteristics](#performance-characteristics)
- [Scalability Considerations](#scalability-considerations)
- [Tradeoffs and Assumptions](#tradeoffs-and-assumptions)
- [API Reference](#api-reference)
- [Setup](#setup)
- [Testing](#testing)
- [Configuration](#configuration)
- [Project Structure](#project-structure)

---

## Problem Statement

A truck with a full tank (50 gallons, 10 MPG) must travel from city A to city B. Fuel stations along the route have different prices. The goal is to minimize total fuel cost for the trip.

This is not a shortest-path problem. A slightly longer route with cheaper station prices can cost less overall. The optimal plan depends on:

- **Station price distribution** — prices vary by $1+/gallon between stations
- **Station density** — dense corridors (I-95, Texas) vs sparse regions (Montana, Dakotas)
- **Detour cost** — leaving the highway adds miles that consume fuel
- **Range constraints** — 500 miles max per tank; must plan stops before running out
- **Strategic tradeoffs** — buying less at a slightly expensive station now to buy more at a cheap station later

The system must evaluate multiple route alternatives, select station stops, determine purchase quantities, and account for all these constraints simultaneously.

---

## Core Features

- **Route alternative comparison**: Google Directions returns up to 2 routes; each is independently optimized for fuel cost
- **Strategic fueling**: three decision strategies evaluated at each candidate stop (fill, partial-to-cheaper, destination-only)
- **Price-aware lookahead**: scans all stations ahead within range to find cheaper options before committing to a purchase
- **Detour-cost modeling**: effective distance includes a 2x penalty on detour miles
- **Four-layer cache architecture**: hot Redis caches, persistent PostgreSQL route cache, in-process geometry LRU, with versioned invalidation
- **Unreachable detection**: never returns `status: "success"` with negative fuel remaining
- **Input hardening**: coordinate type validation, address length limits, null rejection

---

## Architecture Overview

### Layers

```
api.py (DRF endpoint)
  └── engine.py (orchestrator)
       ├── geocoding.py          — Google Geocoding API (parallel via ThreadPoolExecutor)
       ├── routing.py            — Google Directions API (request coalesced)
       └── _standard_optimization_path  (distance > 500 miles)
            ├── PostGIS corridor query  →  stations.py
            ├── FuelOptimizer           →  optimizer.py
            ├── RouteGeometryValidator  →  route_geometry.py
            └── RouteComparator         →  route_selector.py
```

### Data Separation

| Data Type | Storage | Volatility |
|-----------|---------|------------|
| Station locations | PostgreSQL `FuelStation` | Static (preprocessing import) |
| Fuel prices | PostgreSQL `FuelPrice` | Dynamic (versioned, append-only) |
| Price versions | PostgreSQL `PriceVersion` | Dynamic (explicit publish) |
| Route geometry | PostgreSQL `RouteCache` | Semi-static (24h TTL) |
| Optimization results | Redis | Ephemeral (1h TTL) |
| Geocode results | Redis | Ephemeral (7d TTL) |
| Corridor station sets | Redis | Ephemeral (1h TTL) |
| Decoded polylines | In-process `OrderedDict` (max 200) | Process lifetime (LRU) |

### Vehicle Parameters

| Parameter | Value | Constant |
|-----------|-------|----------|
| Tank capacity | 50 gallons | `VEHICLE_TANK` |
| Fuel efficiency | 10 MPG | `VEHICLE_MPG` |
| Max range (full tank) | 500 miles | `VEHICLE_MAX_RANGE` |
| Reserve buffer | 50 miles (5 gallons) | `VEHICLE_RESERVE_MILES` |
| Max detour per stop | 5 miles (round-trip) | `MAX_DETOUR_MILES` |
| Corridor search radius | 50 miles | `CORRIDOR_BUFFER_MILES` |
| Minimum purchase | 5 gallons | (hardcoded in optimizer) |

---

## Request Lifecycle

### Cache Hit Path

1. `engine.py` calls `get_cached_optimization()` with (normalized address, normalized address, price_version)
2. Redis returns cached response
3. `copy.deepcopy()` prevents in-place mutation of cached object (LocMemCache returns references)
4. Per-request fields added (`request_id`, `optimization_time_ms`)
5. Response returned. Zero Google API calls, zero DB queries.

Typical latency: sub-50ms (Redis read + field injection).

### Cache Miss Path

1. **Parallel geocoding**: Start and end locations resolved via Google Geocoding API (2-worker `ThreadPoolExecutor`, 30s timeout). Results cached in Redis for 7 days.
2. **Route fetch**: Google Directions API with `alternatives=true`, up to 2 routes. Request coalescing via Redis lock prevents duplicate API calls. Response cached in `RouteCache` table (24h TTL).
3. **Fast-path check** (line 195): If primary route distance ≤ 500 miles, returns estimated fuel consumption with zero stops. No station queries, no optimization.
4. **Standard path** (distance > 500 miles):
   - Batch-fetch all fuel prices for active price version (1 DB query)
   - Per-route PostGIS corridor query: `ST_DWithin` with GIST index
   - Station-to-route snapping via perpendicular cross-track distance (corrects nearest-point approximation error)
   - Fuel optimizer execution (Greedy + Lookahead)
   - Geographic outlier stop filtering
   - Route comparison via 3-key `min()` selection
   - Response cached in Redis (1h TTL, keyed by normalized inputs + price version)

### Repeat Request

Cache hit path. Identical response (byte-compatible except `request_id` and timing fields).

---

## Fuel Optimization Strategy

### Range-Aware Greedy with Lookahead

The optimizer (`optimizer.py:calculate_fuel_stops`) processes stations in forward distance order from the current position. At each step:

1. **Build candidate list**: Stations within reachable range of current position (current fuel minus 50-mile reserve). Stations >5 miles off-route excluded. Detour cost doubled in effective distance.

2. **Evaluate each candidate in distance order** (first acceptable match wins):

   - **to_destination**: Destination reachable after filling up. Buy only what is needed for the remaining distance plus reserve. Before committing, checks if a cheaper station is reachable directly from current position — if so, skips this station.
   
   - **partial**: Cheaper station exists ahead within full-tank range but is not directly reachable. Buy just enough fuel to reach that cheaper station (including reserve and a 20-mile safety margin).
   
   - **fill**: No cheaper station ahead. Fill tank for maximum flexibility.

3. **Recompute**: After each stop, the optimizer re-evaluates from the new position with updated fuel level. Previously visited stations are tracked via `visited_stations = set()`.

### Lookahead Implementation

The `_station_index` dictionary (`optimizer.py:173-181`) contains ALL stations in the corridor (not just candidates). The lookahead at `optimizer.py:306-320` scans this index for stations that are:

- Ahead of the current candidate's distance
- Within full-tank range of the candidate
- Cheaper than the candidate's price

This is a forward scan across all stations, not just the current candidate list. Stations that become reachable only after filling up at the current candidate are included.

### Safety Constraints

- Minimum 5-gallon purchase prevents micro-stops
- Stops <80 miles after previous fill with >60% fuel remaining are skipped
- 50-mile reserve deducted from usable fuel at all decision points
- If skipping a micro-refuel would leave the route stranded, override to fill
- Station progression is verified monotonic; non-forward stops are removed

---

## Route Selection

`RouteComparator.select_best_route` (`route_selector.py:31-58`) selects the minimum across all route optimizations using a 3-key tuple:

```
primary:   total fuel cost (ascending)       — minimize absolute trip cost
secondary: stop count (ascending)            — fewer stops preferred
tertiary:  route distance (ascending)        — shorter route as tiebreak
```

The `min()` function with a deterministic key ensures stable selection. When both routes have the same cost (extremely unlikely — float costs for different corridors differ measurably), the lower stop count breaks the tie, followed by shorter distance.

All route alternatives are processed independently through the full optimization pipeline before comparison. No route is silently dropped — if Google returns 2 routes, both appear in `route_comparison`.

---

## Cache Architecture

### Layer Details

| Cache Layer | Storage | Key Format | TTL | Invalidated By |
|-------------|---------|------------|-----|----------------|
| Optimization | Redis | `fuel_routing:optimization:v1:{input_hash}:pv{version}` | 1 hour | Price version change (key mismatch) |
| Route geometry | PostgreSQL `RouteCache` | `fuel_routing:route:v1:{coord_hash}` | 24 hours | TTL expiry |
| Geocode | Redis | `fuel_routing:geocode:v1:{address_hash}` | 7 days | TTL expiry |
| Corridor stations | Redis | `fuel_routing:corridor:v1:{id}:{polyline_hash}:buf{mi}` | 1 hour | TTL expiry |
| Polyline geometry | In-process `OrderedDict` (max 200) | Polyline MD5 | Process lifetime | LRU eviction + Redis version check |
| Price version | In-process (30s) + Redis (60s) | `price_version:active` | 30s / 60s | Explicit publish |

### Cache Independence

Price updates change `price_version`. The optimization cache key includes `price_version`, so old cached entries automatically produce a different key → cache miss → recomputation with new prices is triggered naturally.

The following caches do NOT include price version and are unaffected by price changes:

- Route geometry (keyed by coordinates only)
- Geocode results (keyed by normalized address)
- Corridor station sets (keyed by route polyline)
- Polyline geometry (keyed by polyline content)

This separation is intentional: station locations do not change when fuel prices do.

### Request Coalescing

`AtomicCacheOps.get_or_compute` (`cache_utils.py:356-419`) prevents duplicate expensive operations:

1. Check cache → hit? return
2. Acquire Redis lock (`cache.add`, 30s TTL)
3. Double-check cache (another worker may have completed while acquiring lock)
4. Compute and cache result
5. If lock not acquired: poll `wait_for_result` every 100ms for up to 5s → timeout fallback: compute anyway

Applied to: geocoding, route fetching, optimization computation.

---

## PostGIS Strategy

### Corridor Query

```python
qs = FuelStation.objects.filter(
    is_active=True,
    opis_id__in=opis_ids_with_prices,       # pre-filter to ~5000 priced stations
    location_point__dwithin=(route_line, D(mi=CORRIDOR_BUFFER_MILES)),
)
```

- `ST_DWithin` on `PointField` with `geography=True` (geodetic distance calculation in meters)
- GIST index on `location_point` (conditional on `IS NOT NULL`) provides logarithmic spatial selectivity
- `opis_id__in` pre-filter bounds the candidate set to only stations with current prices (the bottleneck is priced stations, not total station count)

### Fallback Path

If PostGIS is unavailable (test environments, migration states), the system falls back to a two-stage filter:

1. Decimal-degree bounding box pre-filter on `latitude`/`longitude` columns
2. Python haversine calculation against sampled polyline waypoints

Results from both paths are cached identically in `CorridorStationCache` (Redis, 1 hour).

### Station Snapping

Stations are snapped to the nearest point on the route polyline using perpendicular cross-track distance (`optimizer.py:snap_station_to_route`). This computes the true distance to each line segment, not the nearest coordinate point.

This matters because Google's `overview_polyline` contains approximately 200 coordinate pairs regardless of route length. The average gap between adjacent coordinates is 13–16 miles, with maximum gaps exceeding 70 miles on straight highway segments. Nearest-point snapping misclassifies stations at the midpoint of a 14-mile gap as ~7 miles from the route, falsely exceeding the 5-mile detour limit. Cross-track distance eliminates this false rejection.

---

## Edge Cases Handled

| Case | Behavior | Location |
|------|----------|----------|
| Source == destination | Returns 0 miles, 0 stops, `status: "success"` | engine.py:195-204 (fast path) |
| Distance ≤ 500 miles | Fast path: estimated consumption, 0 stops, no DB queries | engine.py:195-204 |
| Distance = 500.1 miles | Standard optimization activates, minimal stops added | engine.py:206-212 |
| Station coverage ends before destination | `status: "unreachable"`, `missing_fuel_gallons` reported | engine.py:671+ |
| No stations in corridor at all | `status: "unreachable"`, warning explains gap | engine.py:697-700 |
| Route optimization failure (one alternative) | Per-route exception isolation; remaining route used | engine.py:533-539 |
| Google returns only 1 route | `route_comparison` shows 1 entry; no fabricated route | routing.py:107-126 |
| Price version changes mid-request | Cache key uses DB-fresh version ID, not request-start parameter | engine.py:731, 838-839 |
| Concurrent identical requests | Redis lock + polling ensures single computation | cache_utils.py:356-419 |
| LocMemCache in-place mutation | `copy.deepcopy()` before per-request field writes | engine.py:145 |
| Perpendicular station distance | Cross-track formula recovers stations between sparse polyline points | optimizer.py:110-200 |
| `{"lat": "abc", "lng": "def"}` coordinate abuse | Type-checked at serializer level; returns 400 | serializers.py:190-195 |
| 50000-character address string | max_length=1024 check; returns 400 | serializers.py:185-189 |
| Latitude = 0, longitude = 0 (equator) | Explicitly handled via `is None` check (not `or` falsy) | serializers.py:190-195, engine.py:890-895 |

### Unreachable Route Response

When `available_fuel_gallons < required_fuel_gallons` (with -0.1 gallon floating-point tolerance), the response format changes:

```json
{
  "status": "unreachable",
  "route_feasible": false,
  "selected_route": {
    "route_id": "route_a",
    "distance_miles": 2479.8,
    "is_optimal": false,
    "reason": "Unable to complete route",
    "warning": "Insufficient station coverage for complete optimization. Route cannot be completed."
  },
  "warning": "Route cannot be completed",
  "fuel_stops": [],
  "trip_summary": {
    "total_distance_miles": 2479.8,
    "required_fuel_gallons": 248.0,
    "available_fuel_gallons": 162.1,
    "missing_fuel_gallons": 85.9
  }
}
```

This replaces the previous behavior of returning `status: "success"` with negative `fuel_remaining_at_destination`. The serializer is bypassed for unreachable responses (trip_summary structure differs from success case).

---

## Failure Handling

| Failure | Mechanism | HTTP Status |
|---------|-----------|-------------|
| Google Geocoding timeout | `timeout=(5, 10)` -> `GeocodeFailure` record -> `ValueError` | 400 |
| Google Directions timeout | `timeout=(5, 10)` -> propagates through coalesce lock | 500 |
| No active price version | `PriceVersion.objects.filter(is_active=True).first()` -> `ValueError` | 400 |
| PostGIS exception | `try/except` -> fallback to bbox + Python haversine filter | 200 (degraded) |
| Both corridor query paths fail | Per-route exception isolation -> empty station list | 200 (infeasible) |
| ThreadPoolExecutor geocode timeout | `result(timeout=30)` -> `TimeoutError` -> per-request exception | 500 |
| Rate limit exceeded | DRF `AnonRateThrottle` (1000/hour) | 429 |
| No routes returned by Google | `if not routes_list: raise Exception` | 500 |
| Duplicate route optimization failure | Route A succeeds, Route B fails -> B logged, A used | 200 |
| All routes fail optimization | `select_best_route` receives empty list -> `ValueError` | 500 |

---

## Performance Characteristics

Based on 180 test route observations:

| Metric | Typical Value | Notes |
|--------|---------------|-------|
| Cache hit latency | <50ms | Redis get + per-request field injection |
| Cold short route (<500mi) | ~800-1500ms | Geocoding + Directions API + estimate |
| Cold long route (~1000mi) | ~1000-2500ms | Geocoding + Directions + PostGIS + optimizer |
| Cold coast-to-coast (~3300mi) | ~1500-5000ms | Full pipeline, 10-16 stops |
| Google API calls per request | 3 (cache miss) / 0 (cache hit) | 2 geocoding + 1 directions |
| DB queries per cold request | 3-5 | PriceVersion, RouteCache, FuelPrice avg, station query |

Measured from 180 test runs on the development environment (single-threaded, LocMemCache, Redis disabled). Production deployment behind Gunicorn with Redis will show different characteristics.

---

## Scalability Considerations

- **Geocoding ThreadPoolExecutor**: Module-level `max_workers=2`. In production behind Gunicorn (4-8 workers), each worker has its own executor with independent queue. At 1 request per worker, queue depth ≤ 2, no timeout risk.
- **Route optimization executor**: Per-request `ThreadPoolExecutor` with `max_workers=len(routes)` (max 2), created inside `with` block — automatically closed.
- **ST_DWithin with GIST index**: O(log n) for spatial component. The `opis_id__in` pre-filter is constant (number of priced stations). Mixed-type `static/approx` index.
- **Redis**: All cache keys have explicit TTLs (1h for optimization, 1h for corridor, 7d for geocode). No unbounded growth.
- **RouteCache table**: Entries have `expires_at` field but no automated cleanup. At 10,000 unique routes/day (20,000 rows), annual storage is ~70GB for polyline data — manageable with PostgreSQL but warrants a monthly cleanup job at scale.
- **GeocodeFailure table**: Grows with each failed geocoding attempt. `retry_count ≤ 10` cap limits per-entry retries, but entries are never archived. At high failure rates, monitoring is needed.

---

## Tradeoffs and Assumptions

1. **Greedy over dynamic programming**: Range constraint (500mi) limits the lookahead horizon. True optimality across all station combinations would require DP with O(n²) complexity for negligible real-world savings given typical price variation of <$1/gal between nearby stations.

2. **2 route alternatives**: Google Directions with `alternatives=true` typically returns 1-2 distinct routes. A third alternative was rarely meaningfully different and added ~50% more computation (corridor query + full optimization).

3. **Fixed 50-mile corridor**: Station density along US interstates is such that 50 miles captures all usable stations (max detour is 5 miles). At 5-mile radius, every tested route has sufficient stations for its required stops. The corridor could be tightened to 10-25 miles based on route distance, reducing loaded stations by ~75% and Redis cache footprint proportionally.

4. **Perpendicular over nearest-point distance**: Google's overview polyline has ~200 points regardless of route length. Average coordinate gap is 13-16 miles. Nearest-point snapping reports a station on the route at the midpoint of a 14-mile gap as 7 miles away, exceeding the 5-mile detour limit. Cross-track distance to line segments eliminates this false rejection.

5. **200-waypoint sampling**: Used only for cumulative distance computation in the optimizer. The PostGIS corridor query uses the full LineString geometry — no sampling artifact at the query level.

---

## API Reference

### POST /route/fuel-optimization/

**Request:**

```json
{"start": "Los Angeles, CA", "finish": "New York, NY"}
```

```json
{"start": {"lat": 34.0522, "lng": -118.2437}, "finish": {"lat": 40.7128, "lng": -74.0060}}
```

**Successful response:**
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
    "fuel_remaining_at_destination": 5.0
  }
}
```

---

## Testing

180 integration tests covering 4 distance categories:

| Category | Count | Distance Range | Description |
|----------|-------|----------------|-------------|
| SHORT | 50 | < 500 miles | Fast path, 0 stops expected |
| MED | 50 | 500-1500 miles | 2-4 stops, standard optimization |
| LONG | 50 | 1500-3000 miles | 6-10 stops, multi-state travel |
| COAST | 30 | > 3000 miles | Coast-to-coast, 10-16 stops |

Each test validates:
- **Consumption**: selected route consumption matches trip summary
- **Cost**: selected route cost matches trip summary
- **Accounting**: available = consumed + remaining (within 0.1 gal tolerance)
- **Math**: price × gallons = cost at each stop (within $0.015)
- **Comparison**: selected route cost matches comparison entry

Tests run against a live server via HTTP requests (curl).

```bash
# Start server, then run tests
python test_all.py

# Expected output:
# RESULTS: 180/180 passed | 0/180 failed
```

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Database (PostgreSQL 14+ with PostGIS 3.2+)
createdb fuel_routing_dev
psql fuel_routing_dev -c "CREATE EXTENSION postgis;"
python manage.py migrate
python manage.py preprocess_fuel_data
python manage.py load_fuel_data fuel_prices_cleaned.csv

# Environment
export GOOGLE_MAPS_API_KEY=your_key_here
export REDIS_ENABLED=False
export DATABASE_URL=postgis://user:pass@localhost:5432/fuel_routing_dev

# Development
python manage.py runserver

# Production
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4
export REDIS_ENABLED=True
```

---

## Configuration

Vehicle and optimization parameters in `constants.py` (all configurable via Django settings):

| Setting | Default | Description |
|---------|---------|-------------|
| `VEHICLE_FUEL_EFFICIENCY` | 10 | Miles per gallon |
| `VEHICLE_FUEL_TANK_CAPACITY` | 50 | Gallons |
| `VEHICLE_MAX_RANGE` | 500 | Miles (tank × MPG) |
| `VEHICLE_RESERVE_RANGE_MILES` | 50 | Reserve buffer |
| `FUEL_STOP_CORRIDOR_BUFFER_MILES` | 50 | PostGIS corridor width |
| `FUEL_STOP_MAX_DETOUR_MILES` | 5 | Maximum station detour |

---

## Project Structure

```
fuel_routing/
  api.py              — REST API endpoint (POST /route/fuel-optimization/)
  views.py            — Health check endpoint (GET /route/health/)
  engine.py           — Request orchestrator, response building, unreachable detection
  optimizer.py        — Greedy + Lookahead fuel optimizer, cross-track snapping
  cache_service.py    — Optimization result cache (Redis, price-version keyed)
  cache_utils.py      — Geometry LRU, CorridorStationCache, request coalescing, atomic cache ops
  routing.py          — Google Directions API, RouteCache persistence
  geocoding.py        — Google Geocoding API, failure tracking
  stations.py         — FuelStationQueryService, polyline corridor filtering
  route_selector.py   — RouteComparator, cost-based selection
  route_geometry.py   — RouteGeometryValidator, stop sequence validation
  constants.py        — Vehicle parameters and cache TTLs
  models.py           — FuelStation, FuelPrice, PriceVersion, RouteCache, RouteRequest, GeocodeFailure
  serializers.py      — Request validation (input hardening), response serialization
  preprocessing.py    — CSV preprocessing pipeline (10 stages, 5141 stations)
config/
  settings.py         — Django settings, cache configuration, throttle rates
test_all.py           — 180-route integration test suite
```
