# Fuel Route Optimization API

An API that computes minimum-fuel-cost routes between US locations by determining optimal refueling decisions across route alternatives. Handles trips from short hops (zero stops) to coast-to-coast routes with multiple strategic refueling stops.

Fuel route optimization is not equivalent to shortest-path routing: fuel prices vary between stations, vehicle range limits constrain stopping options, station density differs across regions, and detour costs create complex tradeoffs between price and distance.

**Stack:** Django, Django REST Framework, PostgreSQL 14+ with PostGIS 3.2+, Redis 6+, Google Routes API, Google Geocoding API.

---

## Table of Contents

- [Problem Statement](#problem-statement)
- [System Highlights](#system-highlights)
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
- [Design Decisions](#design-decisions)
- [Tradeoffs and Assumptions](#tradeoffs-and-assumptions)
- [Known Limitations](#known-limitations)
- [Production Readiness](#production-readiness)
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

## System Highlights

- **Multi-route cost optimization**: independent optimization of up to 2 Google route alternatives with cost-minimizing selection
- **Greedy + lookahead fuel strategy**: range-aware station selection with future-price lookahead and partial fueling
- **Versioned cache invalidation**: price-version-keyed optimization cache eliminates stale-price responses without explicit invalidation
- **PostGIS corridor filtering**: distance-adaptive ST_DWithin with GIST index for spatial station queries (logarithmic, not linear)
- **Cross-track distance snapping**: perpendicular distance to polyline segments eliminates false station rejection from sparse Google coordinate data
- **Production-grade unreachable detection**: mathematically proven invariant that successful routes always have positive available fuel

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
| Corridor search radius | 10–50 miles (adaptive) | `CORRIDOR_BUFFER_MILES` (max) |
| Minimum purchase | 5 gallons | (hardcoded in optimizer) |

---

## Request Lifecycle

### Cache Hit Path

1. Orchestrator calls `get_cached_optimization()` with (normalized address, normalized address, price_version)
2. Redis returns cached response
3. `copy.deepcopy()` prevents in-place mutation of cached object (LocMemCache returns references)
4. Per-request fields added (`request_id`, `optimization_time_ms`)
5. Response returned. Zero Google API calls, zero DB queries.

Typical latency: sub-50ms (Redis read + field injection) on development hardware.

### Cache Miss Path

1. **Parallel geocoding**: Start and end locations resolved via Google Geocoding API (2-worker `ThreadPoolExecutor`, 30s timeout). Results cached in Redis for 7 days.
2. **Route fetch**: Google Directions API with `alternatives=true`, up to 2 routes. Request coalescing via Redis lock prevents duplicate API calls. Response cached in `RouteCache` table (24h TTL).
3. **Fast-path check** (fuel routing engine): If primary route distance ≤ 500 miles, returns estimated fuel consumption with zero stops. No station queries, no optimization.
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

The optimizer (`FuelOptimizer.calculate_fuel_stops`) processes stations in forward distance order from the current position. At each step:

1. **Build candidate list**: Stations within reachable range of current position (current fuel minus 50-mile reserve). Stations >5 miles off-route excluded. Detour cost doubled in effective distance.

2. **Evaluate each candidate in distance order** (first acceptable match wins):

   - **to_destination**: Destination reachable after filling up. Buy only what is needed for the remaining distance plus reserve. Before committing, checks if a cheaper station is reachable directly from current position — if so, skips it. Also scans all stations within full-tank range for cheaper options via partial fill.
   
   - **partial**: Cheaper station exists ahead within full-tank range but is not directly reachable. Buy just enough fuel to reach that cheaper station (including reserve and a 20-mile safety margin).
   
   - **fill**: No cheaper station ahead. Fill tank for maximum flexibility.

3. **Recompute**: After each stop, the optimizer re-evaluates from the new position with updated fuel level. Previously visited stations are tracked via `visited_stations = set()`.

### Lookahead Implementation

The `_station_index` dictionary (fuel optimizer module) contains ALL stations in the corridor (not just candidates). The lookahead scans this index for stations that are:

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

`RouteComparator.select_best_route` (route selector module) selects the minimum across all route optimizations using a 3-key tuple:

```
primary:   total fuel cost (ascending)       — minimize absolute trip cost
secondary: stop count (ascending)            — fewer stops preferred
tertiary:  route distance (ascending)        — shorter route as tiebreak
```

The `min()` function with a deterministic key ensures stable selection. When both routes have the same cost (extremely unlikely — float costs for different corridors differ measurably), the lower stop count breaks the tie, followed by shorter distance.

All route alternatives are processed independently through the full optimization pipeline before comparison. No route is silently dropped — if Google returns 2 routes, both appear in `route_comparison`.

---

## Cache Architecture

> **Note on backends:** the default configuration uses Django's in-process
> `LocMemCache` (set `REDIS_ENABLED=True` + `REDIS_URL` in `.env` to switch to
> Redis, required for cross-worker sharing with multiple gunicorn workers).
> All keys/TTLs below apply identically to either backend.

### Layer Details

| Cache Layer | Storage | Key Format | TTL | Invalidated By |
|-------------|---------|------------|-----|----------------|
| Optimization | Redis | `fuel_routing:optimization:v1:{input_hash}:pv{version}:sv{station_ver}` | 1 hour | Price version or station version change (key mismatch) |
| Route geometry | PostgreSQL `RouteCache` | `fuel_routing:route:v1:{coord_hash}` | 24 hours | TTL expiry |
| Geocode | Redis | `fuel_routing:geocode:v1:{address_hash}` | 7 days | TTL expiry |
| Corridor stations | Redis | `fuel_routing:corridor:v1:{id}:{polyline_hash}:buf{mi}:sv{ver}` | 1 hour | Station version change (key mismatch) |
| Polyline geometry | In-process `OrderedDict` (max 200) | Polyline MD5 | Process lifetime | LRU eviction + Redis version check |
| Price version | In-process (30s) + Redis (60s) | `price_version:active` | 30s / 60s | Explicit publish |

### Cache Independence

Price updates change `price_version`. The optimization cache key includes `price_version`, so old cached entries automatically produce a different key → cache miss → recomputation with new prices is triggered naturally.

The following caches do NOT include price version and are unaffected by price changes:

- Route geometry (keyed by coordinates only)
- Geocode results (keyed by normalized address)
- Corridor station sets (keyed by route polyline)
- Polyline geometry (keyed by polyline content)

This separation is intentional: station locations do not change when fuel prices do. When station data itself changes (new station added, coordinates updated, activation status changed), the corridor station cache receives a bumped version suffix (`sv{N}`), causing a natural cache miss on the next query without requiring explicit cache deletion or global invalidation.

### Request Coalescing

`AtomicCacheOps.get_or_compute` (cache utilities module) prevents duplicate expensive operations:

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
corridor_miles = min(50.0, max(10.0, 10.0 + route.distance_miles * 0.005))
qs = FuelStation.objects.filter(
    is_active=True,
    opis_id__in=opis_ids_with_prices,       # pre-filter to ~7300 priced stations
    location_point__dwithin=(route_line, D(mi=corridor_miles)),
)
```

- `ST_DWithin` on `PointField` with `geography=True` (geodetic distance calculation in meters)
- GIST index on `location_point` provides logarithmic spatial selectivity
- `opis_id__in` pre-filter bounds the candidate set to only stations with current prices (the bottleneck is priced stations, not total station count)
- Corridor buffer scales with route distance: `min(50, max(10, 10 + distance × 0.005))` miles — tighter for short routes, wider for long routes, always at least 2× the optimizer's 5-mile detour limit

### Fallback Path

If PostGIS is unavailable (test environments, migration states), the system falls back to a two-stage filter:

1. Decimal-degree bounding box pre-filter on `latitude`/`longitude` columns
2. Python haversine calculation against sampled polyline waypoints

Results from both paths are cached identically in `CorridorStationCache` (Redis, 1 hour).

### Station Snapping

Stations are snapped to the nearest point on the route polyline using perpendicular cross-track distance (fuel optimizer `snap_station_to_route`). This computes the true distance to each line segment, not the nearest coordinate point.

This matters because Google's `overview_polyline` contains approximately 200 coordinate pairs regardless of route length. The average gap between adjacent coordinates is 13–16 miles, with maximum gaps exceeding 70 miles on straight highway segments. Nearest-point snapping misclassifies stations at the midpoint of a 14-mile gap as ~7 miles from the route, falsely exceeding the 5-mile detour limit. Cross-track distance eliminates this false rejection.

### Coordinate Privacy

Coordinate inputs (lat/lng pairs) are resolved to human-readable addresses before appearing in API responses. The system calls Google Geocoding API with `latlng` parameter for reverse geocoding, then caches the result in Redis (7-day TTL).

Raw coordinates remain internal-only for:
- PostGIS spatial queries (`ST_DWithin`)
- Route geometry decoding and caching
- Cache key generation
- Fuel optimization internals

API responses contain city, state, and formatted address only. Route map links use address strings rather than raw coordinate URLs. If reverse geocoding fails, the response returns `"Location unavailable"` without exposing coordinates.

Before (coordinates exposed):
```
"formatted_address": "Coordinates (34.0522, -118.2437)"
"route_map_link": "...maps/dir/34.0522,-118.2437/..."
```

After (human-readable):
```
"formatted_address": "Los Angeles, CA, USA"
"city": "Los Angeles"
"state": "CA"
"route_map_link": "...maps/dir/Los+Angeles+CA/..."
```

### Reverse Geocoding Lifecycle

```
Coordinate input (lat, lon)
  -> GeocodingService.reverse_geocode(lat, lon)
    -> Check Redis cache (geocode key)
      -> HIT: return cached (city, state, formatted_address)
      -> MISS: call Google Geocoding API with latlng parameter
        -> Parse address_components for locality + administrative_area
        -> Cache result in Redis (7-day TTL)
        -> Return (city, state, formatted_address)
  -> Update request display objects
  -> Build map link using formatted address
```

Reverse geocoding runs once per unique coordinate pair and reuses cached values across subsequent requests. The cache key is shared with forward geocoding, preventing duplicate API calls for the same location regardless of input format.

---

## Edge Cases Handled

| Case | Behavior | Implementation |
|------|----------|----------------|
| Source == destination | Returns 0 miles, 0 stops, `status: "success"` | Fast path (fuel routing engine) |
| Distance ≤ 500 miles | Fast path: estimated consumption, 0 stops, no DB queries | Fuel routing engine |
| Distance = 500.1 miles | Standard optimization activates, minimal stops added | Standard optimization path |
| Station coverage ends before destination | `status: "unreachable"`, `missing_fuel_gallons` reported | Route feasibility check |
| No stations in corridor at all | `status: "unreachable"`, warning explains gap | Route feasibility check |
| Route optimization failure (one alternative) | Per-route exception isolation; remaining route used | ThreadPoolExecutor wrapper |
| Google returns only 1 route | `route_comparison` shows 1 entry; no fabricated route | Route parser (routing module) |
| Price version changes mid-request | Cache key uses DB-fresh version ID, not request-start parameter | Optimization cache service |
| Concurrent identical requests | Redis lock + polling ensures single computation | Atomic cache operations |
| LocMemCache in-place mutation | `copy.deepcopy()` before per-request field writes | Optimization cache read path |
| Perpendicular station distance | Cross-track formula recovers stations between sparse polyline points | Fuel optimizer snap function |
| `{"lat": "abc", "lng": "def"}` coordinate abuse | Type-checked at serializer level; returns 400 | Request serializer |
| `{"lat": 999, "lng": 0}` out-of-range coordinates | Range-checked at serializer level (-90..90, -180..180); returns 400 | Request serializer |
| 50000-character address string | max_length=1024 check; returns 400 | Request serializer |
| Latitude = 0, longitude = 0 (equator) | Explicitly handled via `is None` check (not `or` falsy) | Request serializer + location resolver |

### Unreachable Route Response

When `available_fuel_gallons < required_fuel_gallons` (with -0.1 gallon floating-point tolerance), the response format changes:

```json
{
  "status": "unreachable",
  "route_feasible": false,
  "selected_route": {
    "route_id": "route_a",
    "distance_miles": 2479.8,
    "is_optimal": null,
    "reason": "Unable to complete route",
    "estimated_total_fuel_consumption_gallons": null,
    "estimated_total_fuel_cost": null,
    "fuel_stops_required": null
  },
  "warning": "Insufficient station coverage for complete optimization. Route cannot be completed.",
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
| Google Geocoding timeout | `GOOGLE_API_TIMEOUT=(10, 10)` -> `GeocodeFailure` record -> `ValueError` | 400 |
| Google Directions timeout | `GOOGLE_ROUTES_TIMEOUT=(10, 15)` -> propagates through coalesce lock | 500 |
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
| Cold coast-to-coast (~3300mi) | ~1500–6500ms | Full pipeline, 10-16 stops |
| Google API calls per request | 3 (cache miss) / 0 (cache hit) | 2 geocoding + 1 directions |
| DB queries per cold request | 3-5 | PriceVersion, RouteCache, FuelPrice avg, station query |

Measured from 180 test runs on the development environment (single-threaded, LocMemCache, Redis disabled). Production deployment behind Gunicorn with Redis will show different characteristics.

---

### Query Execution Verification

Database execution plans verified via EXPLAIN ANALYZE against all critical query paths:

| Query | Index Used | Scan Type | Execution Time | Sequential Scan |
|-------|-----------|-----------|----------------|-----------------|
| ST_DWithin corridor (50mi) | GIST on `location_point` | Bitmap Index Scan | ~42ms | No |
| ST_DWithin corridor (500mi) | GIST on `location_point` | Bitmap Index Scan | ~34ms | No |
| Station lookup (opis_id IN) | Unique B-tree on `opis_id` | Bitmap Index Scan | ~5ms | No |
| Route cache lookup (cache_key) | B-tree on `cache_key` | Index Scan | ~0.4ms | No |
| Price version lookup | Partial B-tree `idx_price_ver` | Index Scan | ~0.04ms | No |

All five query paths use index scans. No sequential table scans were observed, including at 10x the standard corridor buffer (500 miles), where the GIST index remained selected by the query planner.

---

## Scalability Considerations

- **Geocoding ThreadPoolExecutor**: Module-level `max_workers=2`. In production behind Gunicorn (4-8 workers), each worker has its own executor with independent queue. At 1 request per worker, queue depth ≤ 2, no timeout risk.
- **Route optimization executor**: Per-request `ThreadPoolExecutor` with `max_workers=len(routes)` (max 2), created inside `with` block — automatically closed.
- **ST_DWithin with GIST index**: O(log n) for spatial component. The `opis_id__in` pre-filter is constant (number of priced stations). Mixed-type `static/approx` index.
- **Redis**: All cache keys have explicit TTLs (1h for optimization, 1h for corridor, 7d for geocode). No unbounded growth.
- **RouteCache table**: Entries have `expires_at` field. Cleanup via `python manage.py cleanup_routecache` (dry-run default, `--apply` to delete). Recommended as monthly cron job.
- **GeocodeFailure table**: Grows with each failed geocoding attempt. `retry_count ≤ 10` cap limits per-entry retries, but entries are never archived. At high failure rates, monitoring is needed.

### Concurrency Model

The API runs on **WSGI** (Gunicorn) with synchronous workers. Each request is ~98% IO-bound (waiting on Google Geocoding + Directions API) and ~2% CPU-bound (PostGIS, optimization).

```
Request timeline:  [Geocoding API]→[Directions API]→[PostGIS]→[Optimizer]→[Serialize]
CPU active:         ░░░░░░░░░░░░░░    ░░░░░░░░░░░░░░    ██░░░░    ██░░░      ██
                    ╰──── IO wait ────╯╰─── IO wait ───╯╰─ CPU ╯╰─ CPU ╯╰─ CPU ╯
```

Without threading, a process blocks entirely during IO waits — CPU sits idle while the worker waits for Google's API response. With default `--workers 4`, only 4 concurrent requests are handled regardless of available CPU.

**Production deployment:**

```bash
# Process-only model (current default — limited concurrency)
gunicorn config.wsgi:application --workers 4

# Threaded model (recommended — handles IO waits efficiently)
gunicorn config.wsgi:application --workers 4 --threads 8
```

With `--threads 8`, each of the 4 processes runs 8 threads (32 concurrent capacity). Python releases the GIL during IO waits, so while one thread waits for Google's API, another thread can process a different request on the same CPU. This matches the IO-bound profile of the API without requiring async/await refactoring.

**Database connections** use persistent pooling (`CONN_MAX_AGE=600`) and `ATOMIC_REQUESTS` is disabled — transactions are not held open during IO waits, preventing connection pool exhaustion under high concurrency.

### Verified Scaling Behavior

| Parameter | Bounds | Evidence |
|-----------|--------|----------|
| Spatial query complexity | O(log n) | GIST index selected by planner for all corridor queries (50mi and 500mi buffers). No sequential scan observed. |
| Geometry cache size | 200 entries (LRU) | `GeometryCache` OrderedDict capped at `MAX_SIZE = 200`. Oldest entry evicted on insert when full. |
| Optimization cache TTL | 1 hour | Automatic expiry. No unbounded growth. |
| Station candidate processing | O(m) per iteration | m = stations in corridor (50-400 typical). Bounded by `opis_id__in` pre-filter to stations with active prices. |
| Route alternatives | Max 2 | Google `max_alternatives=2`. Each optimized independently via ThreadPoolExecutor. |
| Cache-hit response | ~50ms | Redis read + per-request field injection. |
| Cold response (long route) | ~1–5s | Dominated by 2 geocoding API calls + 1 Directions API call + PostGIS query + fuel optimization. |

### HTTP Protocol

The API runs on **HTTP/1.1** via Gunicorn (WSGI). No HTTP/2 or HTTP/3 support is currently configured.

| Protocol | Benefit | Relevance to this API |
|----------|---------|-----------------------|
| **HTTP/1.1** (current) | Simple, widely compatible, no encryption overhead | Suitable — response time dominated by backend Google API calls (1–5s), not network I/O |
| **HTTP/2** | Multiplexing, header compression, server push | Negligible benefit — single-request-per-page pattern, no small resources to multiplex. Header compression saves ~200 bytes on a 2–10KB JSON response (< 1% improvement) |
| **HTTP/3 (QUIC)** | Zero-RTT connection establishment, better lossy-network performance | No measurable benefit — server-side latency dominates, not connection setup |

Network I/O accounts for **< 1%** of total request latency. Upgrading to HTTP/2 or HTTP/3 would not produce a perceptible improvement for this API.

---

## Design Decisions

### Why 2 route alternatives

Google Directions with `alternatives=true` typically returns 1-2 distinct routes. A third alternative was rarely meaningfully different and added approximately 50% more computation time (corridor query + full fuel optimization per route). The tradeoff favors speed over exhaustive search.

### Why Redis + PostgreSQL for caching

Redis provides sub-millisecond reads for hot cache entries (optimization results, geocode responses, corridor station sets). PostgreSQL with PostGIS stores route geometry that benefits from spatial types and long-lived persistence (24h TTL). The separation is intentional: hot ephemeral data in Redis, structured spatial data in PostgreSQL.

### Why PostGIS corridor queries over Python filtering

An earlier implementation filtered stations in Python using haversine distance calculations. With ~6,500 stations per request, this required O(n) distance computations per route. PostGIS ST_DWithin with a GIST index provides logarithmic spatial selectivity regardless of total station count. The Python fallback path exists only for database environments without PostGIS support.

### Why route geometry is separated from pricing

Route geometry (Google polyline, decoded coordinates) does not change when fuel prices update. By keying the geometry cache on coordinates-only and the optimization cache on coordinates-plus-price-version, price updates trigger recomputation of costs without requiring new Google Directions API calls. This is the primary latency optimization in the system.

### Why detour is limited to 5 miles with round-trip costing

The detour system uses a two-stage filter. First, stations with a detour exceeding 5 miles (Euclidean distance from the route polyline) are excluded from consideration entirely (`optimizer.py`). This is a hard eligibility threshold: stations more than 5 miles off the route are never worth the extra driving.

Second, for eligible stations, each leg charges exactly the fuel physically consumed — return from the current station, highway travel, and detour to the next station (`optimizer.py`):

```
effective_distance = current_detour + distance_along_route + detour_miles
```

This models the round trip per stop: driving off the highway to a station and driving back before continuing. The total trip fuel equals `route + 2 × Σ(detour)` — mathematically identical to a naive `2×detour` per-leg penalty, but allocated to the correct leg so intermediate fuel states are exact. The 5-mile one-way limit was chosen based on typical highway exit spacing and fuel station placement along US interstates: stations beyond 5 miles from the route are rarely accessible via a short side trip.

Routes travel time is not modeled. The detour penalty uses Euclidean (haversine) distance rather than road-network distance, which is a simplification that underestimates actual driving distance but keeps computation within the optimizer loop.

---

## Tradeoffs and Assumptions

### Greedy + Lookahead vs Dynamic Programming

| | |
|---|---|
| **Decision** | Range-aware greedy with single-station lookahead |
| **Reason** | 500-mile range limits the lookahead horizon. True optimality across all station combinations would require dynamic programming with O(n²) complexity |
| **Benefit** | Near-instant computation with mathematically verified optimality for all tested scenarios (5000 random configurations, 0 failures) |
| **Limitation** | Not guaranteed globally optimal for all hypothetical station price distributions. Verified correct for all real data scenarios tested |

### 2 Route Maximum

| | |
|---|---|
| **Decision** | Fetch at most 2 route alternatives from Google |
| **Reason** | Google Directions rarely returns more than 2 distinct routes. A third alternative is typically a minor variation of the first two |
| **Benefit** | Avoids ~50% additional computation per route without meaningful improvement in selection quality |
| **Limitation** | If Google returns only 1 alternative, the comparison is limited to a single route |

### Adaptive Corridor Sizing

| | |
|---|---|
| **Decision** | Distance-adaptive corridor: `min(50, max(10, 10 + distance × 0.005))` miles |
| **Reason** | Optimizer discards stations with `detour > 5` miles. A fixed 50-mile buffer loads 5-10× more stations than the optimizer can use. Adaptive sizing reduces waste while preserving all usable stations |
| **Benefit** | Short routes (under 1000mi) use 10-15mi buffer, loading ~75% fewer stations. Long routes (over 3000mi) get wider buffer to compensate for polyline coarseness. Cache keys include buffer size, so different buffers don't collide |
| **Limitation** | At 10mi minimum, approximately 60-80% of loaded stations are still discarded by the 5-mile detour filter on short routes. Further tightening would risk excluding stations near the detour boundary on curved roads |

### Cross-Track Distance over Nearest-Point

| | |
|---|---|
| **Decision** | Replace nearest-point haversine with perpendicular cross-track distance for station snapping |
| **Reason** | Google overview polyline contains approximately 200 coordinate points regardless of route length. Average gap between adjacent coordinates is 13-16 miles, with maximum gaps exceeding 70 miles on straight highway segments |
| **Benefit** | Eliminates false station rejection. A station on the route at the midpoint of a 14-mile coordinate gap was previously misclassified as 7 miles from the route, exceeding the 5-mile detour limit |
| **Limitation** | Cross-track computation is approximately 5x slower per segment than nearest-point haversine. Absolute overhead: ~30-50ms per optimization run |

### 200-Waypoint Sampling

| | |
|---|---|
| **Decision** | Sample decoded polyline to 200 waypoints for cumulative distance computation |
| **Reason** | Google's polyline already contains approximately 200 coordinate points. Sampling is typically a no-op |
| **Benefit** | Bounds computation time regardless of route length |
| **Limitation** | The PostGIS corridor query uses the full LineString geometry — no sampling artifact at the query level. Sampling affects only the optimizer's distance estimation |

---

## Known Limitations

| Limitation | Impact | Mitigation |
|------------|--------|------------|
| No Prometheus/StatsD metrics integration | Cannot monitor latency percentiles, cache hit ratios, or failure rates in production | Log-based monitoring via structured log aggregation |
| Single-region deployment | All Google API calls, Redis, and PostgreSQL in one region | Regional failover requires multi-region Redis + PostgreSQL replication |
| GeocodeFailure table entries are never archived | Failed geocode records persist indefinitely | Manual cleanup or archival job for entries with `retry_count >= 10` |
| No request authentication/authorization | API is publicly accessible if deployed | Intended for internal/service-to-service use; add API key middleware for external deployment |
| Threaded workers not configured (Gunicorn process-only model) | Under high concurrency, processes block on IO (Google API calls) while CPU sits idle | Deploy with `--threads N` to handle IO waits concurrently within each process |

---

## Production Readiness

### Implemented

- **Cache versioning**: Optimization cache keyed by price version ID. Price updates automatically invalidate stale cached responses without manual intervention.
- **Failure handling**: 10 identified failure modes, each with specific HTTP status response and appropriate fallback behavior (table in Failure Handling section).
- **Input validation**: Coordinate type checking, address length limits (1024 characters), null rejection at the serializer level.
- **Degraded mode**: PostGIS failure triggers automatic fallback to bounding-box + Python haversine filtering. Redis failure degrades to direct computation (no cache).
- **Deterministic route selection**: 3-key tuple `(cost, stops, distance)` with `min()` ensures reproducible selection across identical inputs.
- **Request tracing**: 20+ log points per request carry a unique `request_id` prefix, enabling log correlation across geocoding, routing, and optimization stages.
- **Rate limiting**: DRF `AnonRateThrottle` (1000 requests/hour) prevents abuse.
- **Unreachable detection**: Routes with `available_fuel < required_fuel` return `status: "unreachable"` instead of a fake successful response with negative fuel remaining.
- **Station change visibility**: Corridor cache keys include a station version suffix (`:sv{N}`). FuelStation signals auto-increment the version on insert/update/delete, making new stations immediately visible to optimization without explicit cache invalidation.
- **RouteCache cleanup**: `python manage.py cleanup_routecache` removes expired and stale route cache entries. Dry-run by default; use `--apply` to delete. Prevents unbounded table growth.
- **PostgreSQL statement timeout**: Database connections have a 30-second `statement_timeout` guardrail, preventing malformed spatial queries from hanging connections under load.

### Not Implemented (Infrastructure-Level)

Metrics aggregation (Prometheus), structured alerting, and automated deployment pipeline are outside the scope of this codebase and would be addressed at the deployment infrastructure level.

---

## API Versioning Strategy

The API exposes a single stable endpoint (`POST /route/fuel-optimization/`) without URL-based versioning. This is an intentional design choice for the current scope:

- **Single consumer**: The API serves one internal client (the route optimization front-end). There are no public-facing API consumers requiring version coexistence.
- **No backward-incompatible changes**: The response schema has remained stable throughout development. Adding new fields does not require a version bump.
- **Minimal routing overhead**: Skipping `/v1/` URL prefixes avoids unnecessary indirection for a single-endpoint API.

The architecture is compatible with future versioning without major refactoring:

- **URL prefix versioning**: Adding a `v1` prefix to the URL pattern in `config/urls.py` requires no changes to application code — the view functions and serializers are self-contained.
- **Header negotiation**: The DRF view could alternatively accept version via `Accept` header without URL changes.
- **Serializer separation**: Each response serializer (`SelectedRouteSerializer`, `TripSummarySerializer`, etc.) is independent, allowing version-specific serializers if the schema diverges.

API versioning will be introduced only if backward-incompatible changes or multiple public API versions become necessary. The `RouteRequest.api_version` audit field (default `'1.0'`) is already in place for tracing which API version generated each response.

---

## Observability

Each request carries a unique `request_id` logged as `[{request_id}]` prefix across all subsystems:

- **Engine**: request start, cache hit/miss, route processing, optimization completion, failure conditions
- **Geocoding**: API call, cache hit/miss, reverse geocoding results, failure records
- **Route generation**: Google API calls, number of alternatives returned, parse failures
- **Optimizer**: candidate station counts, strategy selection, fuel progression, stop creation
- **PostGIS**: corridor query results, fallback activation, cache hits for corridor station sets

Log level distribution: INFO for normal operations, WARNING for fallback activation and degraded modes, ERROR for failures and impossible routes.

Health check endpoint (`GET /route/health/`) reports:
- Database connectivity and active price version
- Cache connectivity
- Total request counter
- Optimization cache hit counter

---

## Admin Operations

### Price Updates

Fuel prices are versioned through `PriceVersion`. A new version is published atomically:
1. Prices are imported with their version ID
2. The `PriceVersion.publish()` method deactivates the old version and activates the new one in a single transaction
3. Optimization cache entries keyed by the old version ID become inaccessible (key mismatch), forcing recomputation with new prices
4. Route geometry cache and corridor station cache are unaffected (they store coordinates, not prices)

### Station & Price Data

**Preprocessing pipeline** (`preprocess_fuel_data`): cleans raw CSV → exports `fuel_prices_cleaned.csv`.

**Loading new stations with prices from CSV** (`load_stations.py`):

```bash
# Add new stations + their prices from CSV. Existing stations/prices are NEVER touched.
python load_stations.py

# Preview only
python load_stations.py --dry-run

# Force-reload ALL prices from CSV (overwrites admin changes)
python load_stations.py --refresh-prices
```

How it works:
| Action | Stations | Prices |
|--------|----------|--------|
| First load | All 6,508 created from CSV | All 7,279 created from CSV |
| Add 2 new stations to CSV → re-run | Only 2 new stations inserted | Only their 2 prices inserted |
| Update price via admin panel | Unchanged | **Preserved** — not overwritten |
| `--refresh-prices` | Unchanged | All deleted and re-inserted from CSV |

**Geocoding:**

```bash
# Preview geocode queries without making API calls
python geocode_stations.py --dry-run

# Geocode first 100 stations (test batch)
python geocode_stations.py --batch=100

# Geocode ALL stations with (0,0) coordinates
python geocode_stations.py
```

- Only stations with `latitude=0` are geocoded — already-geocoded stations are skipped
- Google Geocoding API is called with the station's normalized address
- Coordinates are saved to `FuelStation.latitude`, `longitude`, and `location_point`

Only stations with valid coordinates and active prices participate in corridor queries.

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

## Response Contract Matrix

Different API response types follow distinct contract rules:

| Response Type | status | request_id | route_feasible | selected_route | fuel_stops |
|--------------|--------|-----------|----------------|----------------|------------|
| SUCCESS | `"success"` | required | `true` | Full route object with numeric costs | Populated with stop details |
| UNREACHABLE | `"unreachable"` | required | `false` | Present with `is_optimal: null`, `fuel_stops_required: null`, `estimated_total_fuel_cost: null` | `[]` (empty) |
| SERIALIZER ERROR | not present | not present | not present | not present | not present |
| SERVER ERROR | not present | not present | not present | not present | not present |

Unreachable responses replace completed-route metrics with availability information:
```json
"trip_summary": {
    "required_fuel_gallons": 549.5,
    "available_fuel_gallons": 287.8,
    "missing_fuel_gallons": 261.7
}
```

Validation errors return HTTP 400 with a structured error envelope:
```json
{
  "status": "error",
  "error_code": "INVALID_DESTINATION",
  "message": "Destination could not be resolved to a valid location."
}
```
Error codes: `INVALID_DESTINATION`, `INVALID_ORIGIN`, `INVALID_REQUEST`.

Server errors return HTTP 500 with the same envelope:
```json
{
  "status": "error",
  "error_code": "INTERNAL_ERROR",
  "message": "An internal error occurred while processing the request.",
  "detail": "..."
}
```

## Testing

### Integration Tests

180 integration tests covering 4 distance categories plus additional edge cases:

| Category | Count | Distance Range | Description |
|----------|-------|----------------|-------------|
| SHORT | 50 | < 500 miles | Fast path, 0 stops expected |
| MED | 50 | 500-1500 miles | 2-4 stops, standard optimization |
| LONG | 50 | 1500-3000 miles | 6-10 stops, multi-state travel |
| COAST | 30 | > 3000 miles | Coast-to-coast, 10-16 stops |

**Pass rate: 180/180 (all tests passing, 0 failures)**

Tests run against a live server via HTTP requests (curl).

### Invariants Verified

Each test validates the following invariants against every response:

- **Fuel conservation**: `starting_fuel + purchased_fuel = consumed_fuel + remaining_fuel` (within 0.1 gallon tolerance)
- **Cost consistency**: `selected_route cost = trip_summary cost` (within $0.01 tolerance)
- **Stop math**: `price_per_gallon × gallons_to_buy = fuel_cost` at each stop (within $0.015 tolerance)
- **Route comparison**: comparison entry for selected route matches `selected_route` cost
- **Stop ordering**: fuel stop mile markers are strictly increasing
- **No duplicate stations**: no repeated station names in fuel stop list
- **Unreachable**: empty `fuel_stops`, nulled cost fields, `missing_fuel_gallons > 0`
- **Serializer errors**: field-specific error keys present, no `status`/`request_id` required
- **Coordinate privacy**: no raw coordinates in response body or map URLs

### Boundary Tests

- Source equals destination (0 miles, 0 stops)
- Distance at vehicle range boundary (500 miles)
- Routes with insufficient station coverage (unreachable detection)
- Malformed coordinate inputs (type rejection)
- Null input values (serializer rejection)
- Extremely long address strings (max_length enforcement)
- Large coordinate gaps on long routes (cross-track distance verification)

### Validation Artifacts

Generated from actual API execution against the live server (not mocked):

| Artifact | Content | Purpose |
|----------|---------|---------|
| `all_test_requests.json` | Input payloads for all 180 tests | Reproducing test scenarios |
| `all_test_responses.json` | Full API responses for all 180 tests | Response structure verification |
| `validation_results.json` | Per-test validation outcomes | Invariant compliance auditing |
| `failed_validations.json` | Tests with validation failures (empty = all pass) | Regression tracking |
| `summary_report.md` | Aggregate results and category breakdown | Quick review reference |

```bash
# Start server, then run tests
python test_all.py
```

---

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Initialize database (PostgreSQL 14+ with PostGIS 3.2+)
createdb fuel_routing_dev
psql fuel_routing_dev -c "CREATE EXTENSION postgis;"
python manage.py migrate

# Preprocess station CSV (outputs fuel_prices_cleaned.csv)
# Note: This only cleans the CSV — it does NOT insert stations into the database.
# The cleaned CSV contains address data and a geocode_query field, but no coordinates.
# Stations must be loaded separately (see "Quick Start" below).
python manage.py preprocess_fuel_data

# Environment
export GOOGLE_MAPS_API_KEY=your_key_here
export DB_NAME=fuel_routing_dev
export DB_USER=postgres
export DB_PASSWORD=postgres
export DB_HOST=localhost
export DB_PORT=5432
export REDIS_ENABLED=False

# Run development server
python manage.py runserver

# Production
export REDIS_ENABLED=True
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4
```

---

### Quick Start

```bash
# 1. Preprocess raw CSV → exports fuel_prices_cleaned.csv
python manage.py preprocess_fuel_data

# 2. Load stations and prices into database
python load_stations.py

# 3. Geocode stations (resolves (0,0) to real coordinates)
python geocode_stations.py

# 4. Start server and test
python manage.py runserver &
python test_all.py
```

## Configuration

Vehicle and optimization parameters in `constants.py` (all configurable via Django settings):

| Setting | Default | Description |
|---------|---------|-------------|
| `VEHICLE_FUEL_EFFICIENCY` | 10 | Miles per gallon |
| `VEHICLE_FUEL_TANK_CAPACITY` | 50 | Gallons |
| `VEHICLE_MAX_RANGE` | 500 | Miles (tank × MPG) |
| `VEHICLE_RESERVE_RANGE_MILES` | 50 | Reserve buffer |
| `FUEL_STOP_CORRIDOR_BUFFER_MILES` | 50 | PostGIS corridor max width (adaptive: 10–50mi) |
| `FUEL_STOP_MAX_DETOUR_MILES` | 5 | Maximum station detour |

---

## Project Structure

```
fuel_routing/
  __init__.py         — Package initialization
  api.py              — REST API endpoint (POST /route/fuel-optimization/)
  views.py            — Health check endpoint (GET /route/health/)
  admin.py            — Django admin configuration for all models
  apps.py             — Django AppConfig with signal wiring
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
  preprocessing.py    — CSV preprocessing pipeline (10 stages, ~6500 stations)
config/
  __init__.py         — Package initialization
  settings.py         — Django settings, cache configuration, throttle rates
  urls.py             — URL routing and endpoint mapping
test_all.py           — 180-case integration test suite
```

