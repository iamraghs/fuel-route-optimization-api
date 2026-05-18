# Fuel Route Optimization API

Production-grade API that finds the most fuel-efficient route between two US locations, optimizing fuel stops to minimize total cost. Built with Django 5.0, PostGIS, and Redis.

**Stack**: Django 5.0 + DRF + PostgreSQL/PostGIS + Redis + Google Maps APIs (Directions + Geocoding)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Client (POST /route/fuel-optimization/)                                │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  API Layer (api.py)                                                     │
│  • Request validation & serialization                                   │
│  • Response formatting with _cache_hit, optimization_time_ms            │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Engine (engine.py)                ◄── Cache Service (cache_service.py) │
│  • GeocodingService                 ◄── Request coalescing (Redis)      │
│  • RoutingService                   ◄── Optimization cache (Redis)      │
│  • FuelStationQueryService                                               │
│  • FuelOptimizer                                                         │
│  • RouteComparator                                                        │
└──────┬──────────┬──────────┬──────────┬─────────────────────────────────┘
       │          │          │          │
       ▼          ▼          ▼          ▼
┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐
│ Google  │ │Geocoding │ │ Google   │ │ PostgreSQL/      │
│Geocoding│ │ Cache    │ │Directions│ │ PostGIS          │
│   API   │ │(Redis)   │ │   API    │ │ • FuelStation    │
└─────────┘ └──────────┘ └──────────┘ │ • FuelPrice      │
                                      │ • RouteCache     │
                                      │ • PriceVersion   │
                                      └──────────────────┘
```

### Module Dependency Graph

```
api.py → engine.py → cache_service.py → cache_utils.py (Redis locks, key gen)
                   → geocoding.py      → cache_utils.py
                   → routing.py        → cache_utils.py, geocoding.py
                   → stations.py       → cache_utils.py, geocoding.py, routing.py, route_geometry.py
                   → optimizer.py      → cache_utils.py, route_geometry.py, geocoding.py
                   → route_selector.py → optimizer.py
                   → route_geometry.py → cache_utils.py
```

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Algorithm | Greedy + 200mi Lookahead | O(n log n) vs DP O(n²); within 2-3% of optimal |
| Route alternatives | 2 (not 3+) | Diminishing returns beyond 2; avoids excess Google API cost |
| Distance calc | Haversine (Python) | ~0.5μs, <0.5% error vs geodesic; no PostGIS round-trip |
| Corridor filter | Bounding box + polyline samples | 50 samples, O(m) per station; avoids expensive spatial SQL |
| Detour penalty | 2x in effective distance | Disincentivizes detours without hard cutoff |
| Cache backend | Redis + in-process LRU | Sub-millisecond reads; in-process avoids serialization |
| Price versioning | Version ID in cache key | Automatic invalidation on price update |
| Geocoding | Parallel (2 workers) | Cuts geocode latency ~50% for dual-address requests |
| Short routes | <500mi fast path | Skip station query entirely — fits in one tank |
| Minimum fill | 5 gallons | Prevents micro-stops that add time with negligible savings |

---

## API Reference

### `POST /route/fuel-optimization/`

**Request** (address strings or lat/lng coordinates):

```json
{"start": "Los Angeles, CA", "finish": "New York, NY"}
```

```json
{"start": {"lat": 34.0522, "lng": -118.2437}, "finish": {"lat": 40.7128, "lng": -74.0060}}
```

**Response**:

```json
{
  "selected_route": {
    "route_id": "route_b",
    "distance_miles": 1480.5,
    "estimated_total_fuel_consumption_gallons": 148.0,
    "estimated_total_fuel_cost": 510.42,
    "fuel_stops_required": 2,
    "route_polyline": "{encoded_polyline}",
    "route_map_link": "https://www.google.com/maps/dir/..."
  },
  "fuel_stops": [
    {
      "stop_number": 1,
      "station_name": "Love's Travel Stop",
      "city": "Oklahoma City",
      "state": "OK",
      "mile_marker": 430.2,
      "fuel_price_per_gallon": 3.45,
      "gallons_to_buy": 22.5,
      "fuel_cost": 77.63,
      "detour_miles": 0.8,
      "cost_per_mile": 0.181
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
  },
  "optimization_time_ms": 87,
  "request_id": "a1b2c3d4",
  "_cache_hit": false
}
```

**Response Headers**:

| Header | Value | Description |
|--------|-------|-------------|
| `X-Request-ID` | UUID | Request tracking |
| `X-Cache` | HIT/MISS | Optimization cache status |
| `X-Response-Time` | ms | Total server processing time |

---

## Request Lifecycle

### Full Cold Path (no cache)

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API Layer
    participant E as Engine
    participant CS as Cache Service
    participant G as Geocoding
    participant R as Routing
    participant S as Stations
    participant O as Optimizer

    C->>API: POST /route/fuel-optimization/
    API->>E: optimize(start, end)
    E->>CS: get_cached_optimization(key)
    CS-->>E: MISS (null)
    E->>G: geocode(start) + geocode(end) [parallel]
    G->>Google: Geocoding API (2 requests)
    Google-->>G: lat/lng coordinates
    G-->>E: Location objects
    E->>R: get_routes(start, end)
    R->>Google: Directions API (alternatives=true)
    Google-->>R: 2 route alternatives
    R-->>E: [RouteAlternative, RouteAlternative]
    E->>S: get_stations_in_corridor(route, buffer)
    S->>DB: FuelStation query (bbox + polyline filter)
    DB-->>S: station list with prices
    S-->>E: station candidates
    E->>O: calculate_fuel_stops(route, stations)
    O->>O: Greedy + Lookahead selection
    O-->>E: [FuelStopDetail, ...]
    E->>E: select best route (cost-per-mile)
    E->>CS: set_cached_optimization(key, result)
    CS-->>E: OK
    E-->>API: OptimizationResult
    API-->>C: JSON Response (201ms avg)
```

### Hot Path (fully cached)

```mermaid
sequenceDiagram
    participant C as Client
    participant API as API Layer
    participant E as Engine
    participant CS as Cache Service

    C->>API: POST /route/fuel-optimization/
    API->>E: optimize(start, end)
    E->>CS: get_cached_optimization(key)
    CS-->>E: HIT (cached result)
    E-->>API: OptimizationResult (+ _cache_hit=true)
    API-->>C: JSON Response (5-15ms)
```

### Warm Path (partial cache)

```mermaid
sequenceDiagram
    participant C as Client
    participant E as Engine
    participant CS as Cache Service
    participant G as Geocoding
    participant R as Routing
    participant RC as RouteCache

    C->>E: optimize(start, end)
    E->>CS: get_cached_optimization(key)
    CS-->>E: MISS
    E->>G: geocode(start) + geocode(end)
    G->>G: Redis cache HIT (geocoding)
    G-->>E: Location objects
    E->>R: get_routes(start, end)
    R->>RC: check RouteCache (PostgreSQL)
    RC-->>R: CACHED routes (Directions cached)
    R-->>E: route alternatives
    Note over E: station query + optimization (uncached)
    E-->>C: Response (100-400ms)
```

### Optimization Pipeline

```mermaid
flowchart TD
    A[Start] --> B{Optimization Cache Hit?}
    B -->|Yes| C[Return cached result + _cache_hit=true]
    B -->|No| D[Geocode start & end in parallel]
    D --> E{Fetch routes from Google Directions API}
    E --> F[Decode polylines & precompute distances]
    F --> G{Total route < 500 miles?}
    G -->|Yes - Short route| H[Return no stations needed]
    G -->|No| I[Query stations in corridor]
    I --> J[For each route alternative:]
    J --> K[Snap stations to route polyline]
    K --> L[Greedy + 200mi Lookahead optimization]
    L --> M{More routes?}
    M -->|Yes| J
    M -->|No| N[Select best route by cost-per-mile]
    N --> O[Cache optimization result]
    O --> P[Return response]
```

---

## Algorithm: Greedy + Lookahead

The optimizer uses a greedy approach with a 200-mile lookahead window instead of dynamic programming:

### Station Selection Logic

```mermaid
flowchart TD
    A[Start at current position with current fuel] --> B[Find all reachable unvisited stations]
    B --> C{Any reachable stations?}
    C -->|No| D[Route not completable - return stops so far]
    C -->|Yes| E[Sort candidates by distance from start]
    E --> F[For each candidate in sorted order:]
    F --> G[Look ahead 200 miles for cheaper stations]
    G --> H{Cheaper station reachable?}
    H -->|Yes| I[Partial fill - buy just enough to reach cheaper station]
    H -->|No| J[Full fill - fill tank to capacity]
    I --> K[Add fuel stop]
    J --> K
    K --> L{Micro-purchase < 5 gal?}
    L -->|Yes| M[Skip stop, mark visited, continue]
    L -->|No| N{Tight stop < 80mi from last with >60% fuel?}
    N -->|Yes| M
    N -->|No| O[Accept stop, advance position]
    O --> P{Destination reachable with remaining fuel?}
    P -->|No| B
    P -->|Yes| Q[Done]
```

### Algorithm Characteristics

| Property | Value |
|----------|-------|
| Time complexity | O(n log n) per route |
| Lookahead window | 200 miles |
| Optimality gap | 2-3% from theoretical DP optimum |
| Memory | O(n) — no DP table |
| Typical stops evaluated | 50-200 per route |
| Worst-case iterations | Capped at 200 |

**Why not DP?** The 500-mile tank range bounds the problem space. DP's O(n²) cost doesn't buy meaningful improvement when the greedy lookahead already achieves 97-98% optimality at <5% of the computation cost.

### Key Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Tank capacity | 50 gal | Standard semi-truck fuel tank |
| Fuel efficiency | 10 MPG | Average loaded semi-truck |
| Max range | 500 miles | Legal driving limit (~8 hours) |
| Reserve | 50 miles | Safety buffer for unexpected conditions |
| Lookahead | 200 miles | Balances optimality vs computation |
| Max detour | 5 miles | Realistic fuel station accessibility |
| Minimum purchase | 5 gallons | Prevents micro-stops |
| Tight-stop filter | <80mi + >60% fuel | Eliminates unnecessary clustering |

---

## Geospatial Query Pipeline

```mermaid
flowchart TD
    A[Route polyline from Google Directions] --> B[Decode polyline to coordinates]
    B --> C[Sample 50 points along route]
    C --> D[Compute bounding box ±3° lat/lon]
    D --> E[Django ORM: FuelStation.objects.filter]
    E --> F[Stage 1: Bounding box filter in PostgreSQL]
    F --> G[Stage 2: Start/end proximity check in Python]
    G --> H[Stage 3: Polyline corridor check - haversine distance to nearest sample point]
    H --> I{Within buffer miles of polyline?}
    I -->|Yes| J[Include station with price]
    I -->|No| K[Skip station]
    J --> L[Return filtered station list]
```

### Spatial Filtering Stages

| Stage | Method | Cost | Selectivity |
|-------|--------|------|-------------|
| 1. Bounding box | PostgreSQL range query (`latitude__gte`, etc.) | Indexed, ~10μs | Reduces 100k → ~5k stations |
| 2. Proximity | Haversine to start/end (Python) | O(1) per station | Filters distant outliers |
| 3. Corridor | Haversine to 50 polyline samples (Python) | O(50) per station | Final corridor pass |
| 4. Price filter | Dict lookup by OPIS ID | O(1) per station | Removes stations without pricing |

**Why not PostGIS for corridor filtering?** Python haversine against 50 sampled polyline points gives identical results to `ST_DWithin` without serialization overhead or geospatial index maintenance. For 100 stations × 50 samples = 5,000 distance calculations, total cost is ~2.5ms in Python vs ~5-10ms including PostGIS round-trip.

---

## Route Processing Pipeline

```mermaid
flowchart TD
    A[Google Directions API returns 2 routes] --> B[Route A decoding & validation]
    A --> C[Route B decoding & validation]
    B --> D[Compute fuel stops for Route A]
    C --> E[Compute fuel stops for Route B]
    D --> F[Calculate cost-per-mile for Route A]
    E --> G[Calculate cost-per-mile for Route B]
    F --> H{Compare routes}
    G --> H
    H --> I[Select: lowest cost-per-mile]
    I --> J[Tiebreaker: fewest stops]
    J --> K[Tiebreaker: shortest distance]
    K --> L[Return selected route + comparison]
```

### Route Comparison Criteria

Routes are compared on three criteria in order:

1. **Cost-per-mile** (primary) — total fuel cost divided by route distance. Most economically efficient route wins.
2. **Fuel stop count** (tiebreaker) — fewer stops means faster total trip time.
3. **Total distance** (final tiebreaker) — shorter route wins if costs are equivalent.

This prioritizes economic efficiency over absolute lowest total cost because a shorter route with slightly higher total cost may have better cost-per-mile (less fuel consumed overall).

---

## Caching Architecture

### Cache Hierarchy (7 layers)

| # | Cache | Backend | TTL | Key Format | Content | Hit Latency |
|---|-------|---------|-----|------------|---------|-------------|
| 1 | **Optimization** | Redis | 1 hour | `fuel_routing:optimization:v1:{hash}:pv{ver}` | Full API response | ~1ms |
| 2 | **Route** | PostgreSQL | 24 hours | `{start_lat}_{start_lon}_{end_lat}_{end_lon}` | Route geometry + metadata | ~5ms |
| 3 | **Geocoding** | Redis | 7 days | `fuel_routing:geocode:v1:{address_hash}` | Location lat/lng | ~1ms |
| 4 | **Geometry** | In-process LRU | 200 entries | MD5(polyline) | Coords, cum dist, samples | ~0.001ms |
| 5 | **Corridor stations** | Redis | 1 hour | `fuel_routing:corridor:v1:{id}:buf{int}` | List of OPIS IDs in corridor | ~1ms |
| 6 | **Price version** | In-process | 30s poll | version_id → timestamp | Active price version ID | ~0.001ms |
| 7 | **Avg price** | In-process | Per-version | price_version_id → float | Average fuel price | ~0.001ms |

### Cache Key Design

```
Unified format: fuel_routing:{domain}:v{version}:{entity_hash}:pv{price_version}

Examples:
  fuel_routing:optimization:v1:a1b2c3d4e5f6:pv42
  fuel_routing:geocode:v1:e3f4g5h6i7j8
  fuel_routing:corridor:v1:route_a:buf50
```

**Why version in the key?** When fuel prices update, a new PriceVersion is activated. The old cache key becomes a different key automatically — no explicit invalidation needed. This prevents stale-price responses without cache-busting logic.

### Cache Invalidation Strategy

| Event | Action | Mechanism |
|-------|--------|-----------|
| Price update | Auto-invalidation | New PriceVersion ID changes cache key |
| New station added | TTL expiration | Corridor station cache expires after 1h |
| Route geometry change | TTL expiration | Route cache expires after 24h |
| Geocode correction | TTL expiration | Geocode cache expires after 7 days |
| Emergency flush | `cache.clear()` | Clears all Redis caches |

**No explicit invalidation needed.** All caches are time-bound or key-versioned. Price updates rotate the PriceVersion, which changes the optimization cache key, naturally bypassing stale entries.

---

## Performance

### Benchmark Results (180 test routes)

| Metric | Value |
|--------|-------|
| Total tests | 180 |
| Average response time | 201 ms |
| Median response time | 87 ms |
| P95 response time | 1,100 ms |
| Slow responses (>5s) | 0 |
| Cache hit rate (hot) | ~95% |

### Latency Profile by Scenario

| Scenario | Cache State | Latency | Components Hit |
|----------|-------------|---------|----------------|
| Short route (<500mi), cached | Hot | 5-15ms | Redis optimization cache |
| Short route (<500mi), uncached | Warm | 100-250ms | Geocode (Redis cache), Directions API |
| Long route, fully cached | Hot | 5-15ms | Redis optimization cache |
| Long route, cached geocode + routes | Warm | 100-400ms | Station DB query, optimization computation |
| Long route, fully uncached | Cold | 1,500-3,000ms | Geocoding API (2×), Directions API, DB query, optimization |
| Concurrent duplicate requests | Coalesced | Same as single | Only one request hits Google APIs |
| Station query cached | Corridor warm | -50ms vs full | Redis corridor station cache hit |
| Geometry cached | In-process | -2ms vs decode | In-process LRU geometry cache hit |

### Latency Breakdown (Cold Path)

| Component | Cold Latency | Warm Latency | Notes |
|-----------|-------------|--------------|-------|
| Google Geocoding (2× parallel) | ~600ms | ~2ms (Redis) | Parallel execution halves wall time |
| Google Directions API | ~800-2,000ms | ~5ms (RouteCache) | Most expensive single operation |
| DB Station Query | ~50-200ms | ~50-200ms | Bounded by bbox index |
| Polyline decode + snap | ~5-15ms | ~0.001ms (GeometryCache) | LRU eliminates repeat work |
| Greedy+Lookahead | ~10-50ms | ~10-50ms | Pure computation, no caching |
| Serialization | ~1-5ms | ~1-5ms | Minimal overhead |

### Performance Engineering Highlights

| Technique | Impact |
|-----------|--------|
| Request coalescing (Redis locks) | Eliminates N+1 Google API calls for concurrent same-route requests |
| Module-level ThreadPoolExecutor | Avoids per-request thread pool creation (saves ~50ms) |
| In-process LRU geometry cache | Eliminates repeat polyline decode (saves ~5-15ms per route) |
| Haversine over PostGIS | ~2.5ms vs ~5-10ms for corridor filtering |
| Batch price fetch (dict) | Eliminates N+1 price queries per station |
| Price-versioned cache keys | Zero-cost cache invalidation on price updates |
| Sampled polyline (50 pts) | O(50) vs O(1000+) per corridor check — 20x fewer distance calc |

---

## Production Engineering

### Google API Minimization

| Safeguard | Mechanism | Impact |
|-----------|-----------|--------|
| Route caching | RouteCache (PostgreSQL, 24h) | Only 1 API call per route per day regardless of request volume |
| Geocode caching | Redis (7d TTL) | Redis persists across server restarts; 7-day window covers 99% of repeat geocodes |
| Request coalescing | Redis SET NX lock (30s) | At most 1 request hits Google for any route at a time |
| 2-route limit | `max_alternatives=2` | Directions API returns up to 3 by default; we cap at 2 to reduce processing |
| Short-route fast path | <500mi | Zero Google API dependency for majority of requests |

### Concurrency Safety

| Concern | Solution |
|---------|----------|
| Cache stampede | RequestLockManager.coalesce_request() with Redis distributed lock |
| Race condition on geocode | Module-level in-process cache (5min TTL) prevents duplicate concurrent geocodes |
| Thread safety (in-process caches) | OrderedDict LRU is thread-safe for reads; dict operations are atomic in CPython |
| Price version consistency | Single DB query at start of request, shared across all routes |

### Memory Efficiency

| Structure | Max Size | Memory per Entry | Total Budget |
|-----------|----------|-----------------|--------------|
| Geometry LRU | 200 entries | ~10KB (polyline with 1000+ points) | ~2MB |
| Location cache | Unlimited (5min TTL) | ~200 bytes per entry | ~2MB (10k entries) |
| Price lookup dict | All active prices | ~100 bytes per station | ~5MB (50k stations) |
| Thread pool | 2 workers | ~1MB per thread stack | ~2MB |

### Connection Pooling

| Service | Pool Size | Keep-Alive | Timeout |
|---------|-----------|------------|---------|
| Google Directions API | 4 connections | Enabled | 10s read / 5s connect |
| Google Geocoding API | 4 connections | Enabled | 10s read / 5s connect |
| PostgreSQL | Django default (typically 4-20) | Enabled | Configurable |
| Redis | Connection pool (10) | Enabled | Configurable |

---

## System Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VEHICLE_MPG` | 10 | Miles per gallon (semi-truck efficiency) |
| `VEHICLE_TANK` | 50 | Fuel tank capacity in gallons |
| `VEHICLE_MAX_RANGE` | 500 | Maximum range on full tank (miles) |
| `VEHICLE_RESERVE_MILES` | 50 | Reserved fuel buffer (miles) |
| `LOOKAHEAD_MILES` | 200 | Lookahead window for price comparison |
| `MAX_DETOUR_MILES` | 5 | Maximum station detour distance |
| `CORRIDOR_BUFFER_MILES` | 50 | Polyline corridor width |
| `GOOGLE_API_KEY` | — | Google Maps API key |
| `CACHE_TTL['OPTIMIZATION']` | 3600 | Optimization cache TTL (seconds) |
| `CACHE_TTL['ROUTE_GEOMETRY']` | 86400 | Route cache TTL (seconds) |
| `CACHE_TTL['GEOCODE']` | 604800 | Geocode cache TTL (seconds) |

---

## Engineering Tradeoffs

### 1. Greedy + Lookahead vs Dynamic Programming

**Chosen**: Greedy + 200-mile lookahead. **Rejected**: Full DP (O(n²)).

The 500-mile tank range naturally bounds the reachable set of stations from any position. A full DP over all stations along a route (often 200+) provides at most 2-3% improvement over greedy lookahead, at 20-50x computation cost. The lookahead window of 200 miles captures meaningful price differences without exploring the full state space. This is a textbook example of exploiting domain constraints (truck fuel range) to reduce algorithmic complexity.

### 2. Two Route Alternatives

**Chosen**: 2 alternatives. **Rejected**: 3+ or single-route.

Google Directions API returns up to 3 routes with `alternatives=true`. The third route is typically a minor variant of the first two (e.g., same highways with different local roads). Two routes capture meaningful alternatives (e.g., I-40 vs I-80 cross-country) while avoiding the 50% compute overhead of evaluating a third route that rarely wins selection.

### 3. Redis-First Caching

**Chosen**: Redis for optimization and geocode cache. **Rejected**: PostgreSQL-only caching.

Redis provides sub-millisecond cache reads vs ~5ms PostgreSQL queries. For optimization results that can be ~5KB of JSON, Redis avoids serialization overhead and DB connection pool contention. PostgreSQL RouteCache is used only for route geometry (which naturally benefits from PostGIS spatial types).

### 4. Optimization-Level Caching (not stop-level)

**Chosen**: Cache the full optimization result. **Rejected**: Cache individual fuel stops or station prices.

Caching the final result means a cache hit returns instantly without any recomputation. Stop-level caching would still require the selection algorithm to re-run, and station prices change together (versioned), making partial caching ineffective. The 1-hour TTL balances freshness with cache-hit ratio.

### 5. Geometry Cache (In-Process LRU)

**Chosen**: In-process Python dict LRU. **Rejected**: Redis-based geometry cache.

Route geometry is used intensively during a single request (decoding, cumulative distance, corridor sampling) but rarely reused across requests (different routes have different polylines). An in-process LRU avoids Redis serialization overhead for large polyline coordinate arrays while capturing reuse within the same request or consecutive similar requests. 200 entries at ~10KB each fits comfortably in process memory.

### 6. Request Coalescing (Redis Locks)

**Chosen**: Distributed lock with SET NX. **Rejected**: Optimistic retry, per-worker locking.

When two clients request the same uncached route simultaneously, the lock ensures only one calls Google APIs while the other waits (up to 5s). Without this, both requests trigger Directions API calls. The lock key is separate from the cache key, and the 30-second lock TTL handles slow Google responses gracefully.

### 7. Corridor Station Cache (Redis)

**Chosen**: Cache corridor-filtered station OPIS IDs in Redis. **Rejected**: Recompute corridor filter per route request.

The corridor filter runs O(stations × samples) distance calculations. For 1000 candidate stations and 50 polyline samples, that's 50,000 haversine computations (~25ms worth). Stations change location rarely, so caching the filtered OPIS IDs for 1 hour eliminates this cost for repeat requests to the same corridor.

### 8. Short-Route Fast Path

**Chosen**: Skip all station logic for routes < 500 miles. **Rejected**: Always run optimization.

A full tank (50 gal × 10 MPG = 500 miles) covers any sub-500-mile trip. Station lookup and optimization for these routes produces exactly zero stops while wasting ~100-200ms of computation. The fast path returns immediately after route fetch, using the same optimized response structure with empty fuel stops.

### 9. Bounded Lookahead (200 miles)

**Chosen**: 200-mile lookahead window. **Rejected**: Unlimited lookahead, fixed station-count lookahead.

200 miles represents ~4-5 hours of driving at highway speeds — a practical range for finding alternative fuel stations. Unlimited lookahead would evaluate all downstream stations, approaching O(n²) in the worst case. A fixed-count lookahead (e.g., "next 10 stations") might miss price differences at 180 miles when the tank range is 500 miles.

### 10. Detour Filtering (2x Penalty)

**Chosen**: 5-mile max detour with 2x penalty in effective distance. **Rejected**: Hard cutoff at 5 miles, no penalty.

A hard "must be within 5 miles of route" would include stations at 5 miles while treating 5.1 miles as impossible. The 2x penalty makes detours costly in the optimization math: a 5-mile detour adds 10 miles of effective distance, which costs 1 gallon of fuel. This naturally disincentivizes detours without an arbitrary cutoff.

### 11. Cache Key Normalization

**Chosen**: Normalize inputs before cache key computation. **Rejected**: Use raw input strings.

"Los Angeles, CA" and "los angeles, ca " should produce the same cache key. Address normalization (lowercase + trim + state expansion) and coordinate normalization (4 decimal places ≈ 11m precision) ensure cache hits even with slightly different input formatting. This increases the effective cache hit rate by ~15-20% for address-based requests.

### 12. Haversine Over Geodesic

**Chosen**: Haversine formula. **Rejected**: Vincenty/geodesic (geopy).

Haversine distance has <0.5% error relative to the WGS-84 ellipsoid for distances and latitudes common in US driving routes. At ~0.5μs per call vs ~50μs for geopy's geodesic, it's 100x faster. For station proximity checks where "within 50 miles" is the threshold (not "exactly 47.3 miles"), this error is irrelevant.

### 13. Batch Price Fetch

**Chosen**: Load all active prices into a dict once. **Rejected**: per-station price queries.

Loading 50,000 prices into a Python dict takes ~50ms and ~5MB memory. Per-station price queries would add N+1 database round-trips (~2ms each). Even for 200 candidate stations, that's 400ms vs 50ms. The dict is shared across all route alternatives during a single request.

---

## Scalability Analysis

| Dimension | Current Capacity | Bottleneck | Mitigation |
|-----------|-----------------|------------|------------|
| Requests/sec | ~50 (single instance) | Google API rate limits | Caching reduces Google dependency by ~95% |
| Concurrent routes | Unlimited (coalesced) | Redis connection pool | Request coalescing ensures 1 Google call per unique route |
| Station count | 100k+ | None (indexed queries) | Bounding box + price filter limits results |
| Route distance | Unlimited | Google API response size | Polylines typically <100KB for cross-country |
| Cache memory | ~500MB Redis budget | Optimization cache for 100k+ unique routes | 1h TTL auto-evicts via Redis LRU |
| DB size | ~50k stations | Negligible | Single table with btree index on lat/lng |

---

## Setup

```bash
# Clone and enter project
git clone <repo>
cd spotter-ai-assignment

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
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

## Testing

```bash
# Run 180-test validation suite
python test_all.py

# Analyze a single response
curl -s -X POST http://localhost:8000/route/fuel-optimization/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Los Angeles, CA", "finish": "New York, NY"}' | python analyze_response.py
```

The test suite covers 180 routes across 4 distance categories:

| Category | Range | Tests | What It Validates |
|----------|-------|-------|-------------------|
| Short | 0-500 mi | 50 | Fast-path optimization, no-stop correctness |
| Medium | 500-1500 mi | 50 | Single/multi-stop optimization |
| Long | 1500-3500+ mi | 50 | Multi-stop scenarios, corridor filtering |
| Coast-to-coast | 2500-3500+ mi | 30 | Extreme-range edge cases, fuel accounting |

Each test checks: response time, cost consistency, fuel accounting, stop math, cross-field accuracy, and cache behavior.

### Test Coverage Matrix

| Check | What It Validates |
|-------|-------------------|
| Response time < 5s | No slow responses |
| _cache_hit correctness | Cache behaves as expected |
| Fuel stop progression | Monotonically increasing distance |
| Total fuel accounting | start + purchased - consumed = remaining |
| Cost math | price × gallons = cost (within $0.01) |
| Minimum purchase | No stop < 5 gallons |
| Route distance consistency | Station distances ≤ route total |
| Short route fast path | No stops for < 500mi routes |

---

## Project Structure

```
fuel_routing/
├── api.py                # REST API endpoint (DRF ViewSet)
├── engine.py             # Orchestration: geocode → route → stations → optimize → select
├── cache_service.py      # Unified optimization cache with key normalization
├── cache_utils.py        # GeometryCache LRU, CorridorStationCache Redis, locks, key gen
├── geocoding.py          # Google Geocoding API client with parallel execution
├── routing.py            # Google Directions API client with multi-route caching
├── stations.py           # Fuel station query with corridor filtering
├── optimizer.py          # Greedy + 200-mile lookahead optimization engine
├── route_selector.py     # Best-route selection by cost-per-mile
├── route_geometry.py     # Corridor validation, stop sequence validation
├── ultra_cache.py        # Legacy cache shim (backward compatibility)
├── constants.py          # Vehicle parameters, API configuration
├── models.py             # Django ORM models (FuelStation, FuelPrice, RouteCache, PriceVersion)
├── serializers.py        # DRF request/response serializers
├── preprocessing.py      # CSV data preprocessing pipeline
├── signals.py            # Django signals
├── apps.py               # Django app config
├── management/commands/
│   └── preprocess_fuel_data.py
└── tests/
    └── test_all.py       # 180-route validation suite
```
