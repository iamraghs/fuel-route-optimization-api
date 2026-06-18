"""Main route + fuel optimization orchestrator."""
import copy
import hashlib
import logging
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from django.contrib.gis.geos import LineString
from django.contrib.gis.measure import D
from django.core.cache import cache

from .cache_utils import (
    CorridorStationCache,
)
from .cache_service import get_cached_optimization, set_cached_optimization
from .constants import (
    GOOGLE_API_KEY, VEHICLE_MAX_RANGE, VEHICLE_MPG, VEHICLE_TANK,
    MIN_DESTINATION_RESERVE_GALLONS,
    CORRIDOR_BUFFER_MILES,
)
from .geocoding import GeocodingService, Location
from .models import FuelPrice, FuelStation, PriceVersion
from .optimizer import FuelOptimizer, get_cached_avg_price
from .route_geometry import RouteGeometryValidator, decode_route_to_coordinates
from .route_selector import RouteComparator
from .routing import RoutingService
from .stations import FuelStationQueryService

logger = logging.getLogger(__name__)


# In-process cache for active price version: {ttl_timestamp: version_id}
_active_price_version_cache: Dict[str, Any] = {
    'version_id': 0,
    'cached_at': 0.0,
}
_PRICE_VERSION_CACHE_TTL = 30  # seconds

# Module-level ThreadPoolExecutor for parallel geocoding (reused across requests)
_geocode_executor = ThreadPoolExecutor(max_workers=2)

# Lightweight cache-hit counters for observability
_cache_hit_counters: Dict[str, int] = {}
_request_counter: int = 0
_STATS_LOG_INTERVAL = 100  # Log aggregate stats every N requests


def _get_active_price_version() -> int:
    """Get current active price version ID with in-process caching + Redis sync."""
    now = time.time()
    if (_active_price_version_cache['cached_at'] + _PRICE_VERSION_CACHE_TTL) > now:
        return _active_price_version_cache['version_id']

    try:
        # Check Redis first for cross-worker consistency
        redis_version = cache.get('price_version:active')
        if redis_version is not None:
            _active_price_version_cache['version_id'] = redis_version
            _active_price_version_cache['cached_at'] = now
            return redis_version

        pv = PriceVersion.objects.filter(is_active=True).values_list('id', flat=True).first()
        version_id = pv or 0
        cache.set('price_version:active', version_id, 60)  # Sync to other workers
        _active_price_version_cache['version_id'] = version_id
        _active_price_version_cache['cached_at'] = now
        return version_id
    except Exception:
        return 0


def _get_avg_price_for_version(price_version_id: int) -> float:
    """Get average fuel price using in-process cache."""
    return get_cached_avg_price(price_version_id)


def _build_map_link(start_input, end_input, start_loc, end_loc) -> str:
    """Build Google Maps URL using formatted address when available, else coordinates."""
    def _encode(val):
        if isinstance(val, str):
            return urllib.parse.quote(val)
        return None

    origin = _encode(start_input) if isinstance(start_input, str) else None
    dest = _encode(end_input) if isinstance(end_input, str) else None
    if origin and dest:
        return f"https://www.google.com/maps/dir/{origin}/{dest}"

    start_coords = f"{start_loc.latitude},{start_loc.longitude}"
    end_coords = f"{end_loc.latitude},{end_loc.longitude}"
    return f"https://www.google.com/maps/dir/{start_coords}/{end_coords}"


class FuelRouteOptimizationEngine:
    """Main orchestrator for complete route + fuel optimization."""

    @staticmethod
    def _parse_location_for_display(location_input: str | Dict[str, float]) -> Tuple[str, str, str]:
        """Parse location input to extract city, state, and formatted address."""
        if isinstance(location_input, dict):
            return ('Unknown', 'US',
                    f"Coordinates ({location_input.get('lat', 0):.4f}, {location_input.get('lng', 0):.4f})")

        address = str(location_input).strip()
        parts = [p.strip() for p in address.split(',')]

        if len(parts) >= 2:
            state = parts[-1]
            city = parts[-2]
        elif len(parts) == 1:
            city = parts[0]
            state = 'US'
        else:
            city = 'Unknown'
            state = 'US'

        formatted_address = address
        return (city, state, formatted_address)

    @staticmethod
    def optimize(
        start_input: str | Dict[str, float],
        end_input: str | Dict[str, float]
    ) -> Dict[str, Any]:
        """Complete route + fuel optimization workflow."""
        start_time = time.time()
        request_id = hashlib.md5(
            f"{start_input}{end_input}{time.time()}".encode()
        ).hexdigest()[:8]

        logger.info(f"[{request_id}] Starting optimization: {start_input} -> {end_input}")

        try:
            # Get active price version (cached in-process to avoid repeated DB queries)
            pv = _get_active_price_version()

            # Check unified optimization cache first (Redis, price-version-aware)
            cached_result = get_cached_optimization(start_input, end_input, price_version=pv)
            if cached_result is not None:
                # Deep copy to prevent in-place mutation of cached object
                # (LocMemCache returns references; Redis serializes so this is a no-op there)
                cached_result = copy.deepcopy(cached_result)
                # Add per-request fields (not stored in cache)
                elapsed_ms = int((time.time() - start_time) * 1000)
                cached_result['optimization_time_ms'] = elapsed_ms
                cached_result['request_id'] = request_id
                # Backward compat: ensure new fields exist on older cached responses
                cached_result.setdefault('route_feasible', True)
                cached_result.setdefault('optimization_confidence', 'cached')
                _cache_hit_counters['optimization'] = _cache_hit_counters.get('optimization', 0) + 1
                global _request_counter
                _request_counter += 1
                logger.info(
                    f"[{request_id}] Optimization cache HIT in {elapsed_ms}ms"
                )
                if _request_counter % _STATS_LOG_INTERVAL == 0:
                    logger.info(
                        f"[STATS] requests={_request_counter} "
                        f"cache_hits={_cache_hit_counters.get('optimization', 0)}"
                    )
                return cached_result

            # Validate API key
            if not GOOGLE_API_KEY or GOOGLE_API_KEY == 'your_google_maps_api_key_here':
                raise ValueError(
                    "Google Maps API key not configured. "
                    "Set GOOGLE_MAPS_API_KEY in your environment or .env file."
                )

            # Parse location display info
            start_city, start_state, start_formatted = \
                FuelRouteOptimizationEngine._parse_location_for_display(start_input)
            end_city, end_state, end_formatted = \
                FuelRouteOptimizationEngine._parse_location_for_display(end_input)

            # Resolve locations in parallel using module-level executor
            start_future = _geocode_executor.submit(
                FuelRouteOptimizationEngine._resolve_location, start_input
            )
            end_future = _geocode_executor.submit(
                FuelRouteOptimizationEngine._resolve_location, end_input
            )
            start_loc = start_future.result(timeout=30)
            end_loc = end_future.result(timeout=30)

            if not start_loc or not end_loc:
                raise ValueError("Could not geocode start or end location")

            # Get routes from Google API
            routes = RoutingService.get_routes(start_loc, end_loc, max_alternatives=2)
            if not routes:
                raise ValueError("No routes found from Google API")

            # Short route fast-path
            primary_route = routes[0]
            if primary_route.distance_miles <= VEHICLE_MAX_RANGE:
                result = FuelRouteOptimizationEngine._short_route_path(
                    routes, start_input, end_input, start_loc, end_loc,
                    start_city, start_state, start_formatted,
                    end_city, end_state, end_formatted,
                    request_id, start_time
                )
                # Cache short-route results too (they don't depend on station data)
                set_cached_optimization(start_input, end_input, result, price_version=pv)
                return result

            # Standard optimization path
            return FuelRouteOptimizationEngine._standard_optimization_path(
                routes, start_input, end_input,
                start_loc, end_loc,
                start_city, start_state, start_formatted,
                end_city, end_state, end_formatted,
                request_id, start_time, pv
            )

        except Exception as e:
            logger.error(f"[{request_id}] Optimization failed: {e}")
            raise

    @staticmethod
    def _short_route_path(
        routes: List['RouteAlternative'],
        start_input, end_input,
        start_loc: Location, end_loc: Location,
        start_city: str, start_state: str, start_formatted: str,
        end_city: str, end_state: str, end_formatted: str,
        request_id: str, start_time: float
    ) -> Dict[str, Any]:
        """Handle routes within a single tank of fuel (<500 miles)."""
        pv = _get_active_price_version()
        avg_price_per_gal = _get_avg_price_for_version(pv)

        best_route = min(routes, key=lambda r: r.distance_miles)
        elapsed_ms = int((time.time() - start_time) * 1000)

        route_map_link = _build_map_link(start_input, end_input, start_loc, end_loc)

        estimated_fuel = best_route.distance_miles / VEHICLE_MPG
        estimated_cost = estimated_fuel * avg_price_per_gal

        response = {
            '_cache_hit': False,
            'route_feasible': True,
            'optimization_confidence': 'high',
            'status': 'success',
            'request_id': request_id,
            'optimization_time_ms': elapsed_ms,
            'request': {
                'start': {
                    'city': start_city,
                    'state': start_state,
                    'formatted_address': start_formatted
                },
                'finish': {
                    'city': end_city,
                    'state': end_state,
                    'formatted_address': end_formatted
                }
            },
            'selected_route': {
                'route_id': best_route.route_id,
                'distance_miles': round(best_route.distance_miles, 1),
                'is_optimal': True,
                'reason': 'Shortest distance (full tank sufficient)',
                'estimated_total_fuel_consumption_gallons': round(estimated_fuel, 1),
                'estimated_total_fuel_cost': float(estimated_cost),
                'fuel_stops_required': 0,
                'route_polyline': best_route.polyline_encoded,
                'route_map_link': route_map_link,
            },
            'route_comparison': [
                {
                    'route_id': r.route_id,
                    'distance_miles': round(r.distance_miles, 1),
                    'estimated_total_fuel_cost': float(
                        (r.distance_miles / VEHICLE_MPG) * avg_price_per_gal
                    ),
                    'fuel_stops_required': 0,
                    'selected': r.route_id == best_route.route_id
                }
                for r in routes
            ],
            'fuel_stops': [],
            'trip_summary': {
                'total_distance_miles': round(best_route.distance_miles, 1),
                'total_fuel_consumed_gallons': round(estimated_fuel, 1),
                'total_fuel_cost': float(estimated_cost),
                'average_price_per_gallon': round(avg_price_per_gal, 3),
                'total_fuel_stops': 0,
                'starting_fuel_gallons': VEHICLE_TANK,
                'fuel_purchased_at_stops': 0.0,
                'total_fuel_available': round(VEHICLE_TANK, 1),
                'fuel_remaining_at_destination': round(VEHICLE_TANK - estimated_fuel, 1),
            }
        }

        logger.info(
            f"[{request_id}] Fast-path complete in {elapsed_ms}ms "
            f"(skipped fuel station queries)"
        )
        return response

    @staticmethod
    def _standard_optimization_path(
        routes, start_input, end_input,
        start_loc: Location, end_loc: Location,
        start_city: str, start_state: str, start_formatted: str,
        end_city: str, end_state: str, end_formatted: str,
        request_id: str, start_time: float, pv: int
    ) -> Dict[str, Any]:
        """Handle routes requiring fuel stops (>500 miles)."""
        logger.info(f"[{request_id}] Processing {len(routes)} routes independently...")

        # Batch fetch all prices once (reuses price version from cache)
        price_version_obj = PriceVersion.objects.filter(is_active=True).first()
        if not price_version_obj:
            raise ValueError("No active price version found")

        all_prices = dict(
            FuelPrice.objects.filter(version=price_version_obj)
            .values_list('opis_id', 'price_per_gallon')
        )
        logger.info(f"[{request_id}] Pre-fetched {len(all_prices)} prices")

        # Merged bounding box for single station query
        try:
            all_lats = []
            all_lons = []

            for r in routes:
                bounds = r.bounds if isinstance(r.bounds, dict) else {}
                sw = bounds.get('sw', {})
                ne = bounds.get('ne', {})

                sw_lat = sw.get('lat')
                if sw_lat is None:
                    sw_lat = sw.get('latitude')
                sw_lon = sw.get('lng')
                if sw_lon is None:
                    sw_lon = sw.get('longitude')
                ne_lat = ne.get('lat')
                if ne_lat is None:
                    ne_lat = ne.get('latitude')
                ne_lon = ne.get('lng')
                if ne_lon is None:
                    ne_lon = ne.get('longitude')

                if all([sw_lat is not None, sw_lon is not None,
                        ne_lat is not None, ne_lon is not None]):
                    all_lats.extend([sw_lat, ne_lat])
                    all_lons.extend([sw_lon, ne_lon])

            if not all_lats or not all_lons:
                raise ValueError("No valid bounds extracted")

            merged_lat_min = min(all_lats)
            merged_lat_max = max(all_lats)
            merged_lon_min = min(all_lons)
            merged_lon_max = max(all_lons)

            lat_range = merged_lat_max - merged_lat_min
            lon_range = merged_lon_max - merged_lon_min
            if lat_range > 50 or lon_range > 50:
                logger.warning(f"Bounds suspiciously large: lat_range={lat_range}, lon_range={lon_range}")
        except Exception as e:
            logger.warning(f"Failed to calculate merged bounds: {e} - using start/end")
            merged_lat_min = min(start_loc.latitude, end_loc.latitude)
            merged_lat_max = max(start_loc.latitude, end_loc.latitude)
            merged_lon_min = min(start_loc.longitude, end_loc.longitude)
            merged_lon_max = max(start_loc.longitude, end_loc.longitude)

        opis_ids_with_prices = list(all_prices.keys())

        # OPTIMIZED: Per-route PostGIS corridor query with automatic fallback
        def _load_route_stations(route):
            """Load stations for a route using PostGIS corridor query (with fallback)."""
            cached_ids = CorridorStationCache.get(route.route_id, CORRIDOR_BUFFER_MILES, route.polyline_encoded or '')
            if cached_ids is not None:
                db_stations = FuelStation.objects.filter(
                    opis_id__in=list(cached_ids), is_active=True
                ).values(
                    'opis_id', 'name', 'address', 'city', 'state', 'latitude', 'longitude'
                )
                result = []
                for s in db_stations:
                    price = all_prices.get(s['opis_id'])
                    if price and price > 0:
                        s['price_per_gallon'] = price
                        s['latitude'] = float(s['latitude'])
                        s['longitude'] = float(s['longitude'])
                        result.append(s)
                return result

            if route.polyline_encoded:
                try:
                    all_coords, _, _, _ = FuelOptimizer.precompute_route_distances(
                        route.polyline_encoded
                    )
                    if all_coords:
                        route_line = LineString(
                            [(float(lon), float(lat)) for lat, lon in all_coords], srid=4326
                        )
                        qs = FuelStation.objects.filter(
                            is_active=True,
                            opis_id__in=opis_ids_with_prices,
                            location_point__dwithin=(route_line, D(mi=CORRIDOR_BUFFER_MILES)),
                        ).values(
                            'opis_id', 'name', 'address', 'city', 'state', 'latitude', 'longitude'
                        )
                        result = []
                        for s in qs.iterator():
                            price = all_prices.get(s['opis_id'])
                            if price and price > 0:
                                s['price_per_gallon'] = price
                                s['latitude'] = float(s['latitude'])
                                s['longitude'] = float(s['longitude'])
                                result.append(s)
                        CorridorStationCache.set(
                            route.route_id, CORRIDOR_BUFFER_MILES,
                            [s['opis_id'] for s in result],
                            route.polyline_encoded or ''
                        )
                        return result
                except Exception:
                    logger.warning(
                        f"[{request_id}] PostGIS corridor query failed for {route.route_id}, "
                        f"falling back to bbox+Python filter"
                    )

            # Fallback: load bbox and Python-filter per route (self-contained, no shared state)
            try:
                bounds = route.bounds if isinstance(route.bounds, dict) else {}
                sw, ne = bounds.get('sw', {}), bounds.get('ne', {})
                slat = sw.get('lat')
                if slat is None:
                    slat = sw.get('latitude')
                slon = sw.get('lng')
                if slon is None:
                    slon = sw.get('longitude')
                nelat = ne.get('lat')
                if nelat is None:
                    nelat = ne.get('latitude')
                nelon = ne.get('lng')
                if nelon is None:
                    nelon = ne.get('longitude')
                if all(v is not None for v in [slat, slon, nelat, nelon]):
                    lat_pad, lon_pad = 3.0, 3.0
                    lat_min, lat_max = min(slat, nelat), max(slat, nelat)
                    lon_min, lon_max = min(slon, nelon), max(slon, nelon)
                    route_bbox_stations = []
                    for st in FuelStation.objects.filter(
                        is_active=True, opis_id__in=opis_ids_with_prices,
                        latitude__gte=lat_min - lat_pad, latitude__lte=lat_max + lat_pad,
                        longitude__gte=lon_min - lon_pad, longitude__lte=lon_max + lon_pad,
                    ).values(
                        'opis_id', 'name', 'address', 'city', 'state', 'latitude', 'longitude'
                    ).iterator():
                        price = all_prices.get(st['opis_id'])
                        if price and price > 0:
                            st['price_per_gallon'] = price
                            st['latitude'] = float(st['latitude'])
                            st['longitude'] = float(st['longitude'])
                            route_bbox_stations.append(dict(st))
                    return FuelStationQueryService.filter_stations_by_route(
                        route, route_bbox_stations
                    ) if route_bbox_stations else []
            except Exception as e:
                logger.warning(f"[{request_id}] Fallback station query failed: {e}")

            return []

        # Optimize each route — run corridor queries and fuel optimization in parallel
        route_optimizations = []

        def _optimize_single_route(route):
            """Load stations, pre-snap, and calculate fuel stops for one route."""
            route_stations = _load_route_stations(route)

            # Pre-snap stations to route
            if route_stations and route.polyline_encoded:
                all_coords, cum_distances, sampled_coords, sampled_cum_dist = \
                    FuelOptimizer.precompute_route_distances(route.polyline_encoded)
                for station in route_stations:
                    snapped_dist, detour = FuelOptimizer.snap_station_to_route(
                        float(station['latitude']),
                        float(station['longitude']),
                        all_coords,
                        cum_distances
                    )
                    station['_snapped_distance'] = snapped_dist
                    station['_detour_miles'] = detour

            if not route_stations:
                stops = []
            else:
                stops = FuelOptimizer.calculate_fuel_stops(
                    route, route_stations, start_loc, end_loc
                )

            # Compute total cost for this route
            # Actual stop costs for feasible routes; full distance-based estimate for infeasible.
            # This keeps selection consistent with the comparison display — without this,
            # a route with one cheap stop (partial coverage) unfairly beats a shorter route
            # that would actually cost less for the full trip.
            if route.distance_miles > VEHICLE_MAX_RANGE:
                if not stops:
                    # No stations found — use full distance estimate
                    pv_local = _get_active_price_version()
                    ap_local = get_cached_avg_price(pv_local)
                    total_cost = (route.distance_miles / VEHICLE_MPG) * ap_local
                else:
                    stop_cost = float(sum(s.fuel_cost for s in stops))
                    last_stop_fuel_after = float(stops[-1].fuel_after_refuel)
                    last_stop_dist = stops[-1].distance_from_start
                    final_leg_miles = route.distance_miles - last_stop_dist
                    if last_stop_fuel_after < final_leg_miles / VEHICLE_MPG:
                        # Stops don't cover full distance — use full estimate for fair comparison
                        pv_local = _get_active_price_version()
                        ap_local = get_cached_avg_price(pv_local)
                        total_cost = (route.distance_miles / VEHICLE_MPG) * ap_local
                    else:
                        total_cost = stop_cost
            else:
                total_cost = float(sum(s.fuel_cost for s in stops))
            return route, stops, total_cost

        routes_list = list(routes)
        with ThreadPoolExecutor(max_workers=len(routes_list)) as executor:
            futures = {executor.submit(_optimize_single_route, r): r for r in routes_list}
            for future in as_completed(futures):
                try:
                    opt_route, opt_stops, opt_cost = future.result()
                    route_optimizations.append((opt_route, opt_stops, opt_cost))
                    logger.info(
                        f"[{request_id}] {opt_route.route_id} complete: "
                        f"{len(opt_stops)} stops, ${opt_cost:.2f}"
                    )
                except Exception as e:
                    logger.error(f"[{request_id}] Route optimization failed: {e}")

        # Select best route
        selected_route, selected_stops, selected_cost = \
            RouteComparator.select_best_route(route_optimizations)

        # Filter geographic outlier stops
        polyline_coords_check = decode_route_to_coordinates(
            selected_route.polyline_encoded
        ) if selected_route.polyline_encoded else []

        selected_stops_cleaned = RouteGeometryValidator.reject_invalid_sequences(
            selected_stops, polyline_coords_check, selected_route.distance_miles
        )

        if len(selected_stops_cleaned) < len(selected_stops):
            removed = len(selected_stops) - len(selected_stops_cleaned)
            logger.info(f"[{request_id}] Removed {removed} geographic outlier stops")

            fuel_purchased_cleaned = sum(
                round(s.gallons_to_buy, 1) for s in selected_stops_cleaned
            )
            fuel_available_cleaned = VEHICLE_TANK + fuel_purchased_cleaned
            fuel_consumed = selected_route.distance_miles / VEHICLE_MPG
            fuel_remaining_cleaned = fuel_available_cleaned - fuel_consumed

            if fuel_remaining_cleaned < 0:
                logger.warning(
                    f"[{request_id}] Filtering would cause insufficient fuel "
                    f"({fuel_remaining_cleaned:.1f}gal deficit). Keeping all stops."
                )
            else:
                selected_stops = selected_stops_cleaned
        else:
            selected_stops = selected_stops_cleaned

        # Validate route geometry
        if selected_stops and selected_route.distance_miles > 500:
            is_valid, error_msg = \
                FuelRouteOptimizationEngine._validate_route_geometry(
                    selected_stops, selected_route, request_id
                )
            if not is_valid:
                logger.error(f"[{request_id}] Route geometry invalid: {error_msg}")

        # Build response
        elapsed_ms = int((time.time() - start_time) * 1000)

        fuel_stops_response = []
        response_total_cost = 0.0
        for i, s in enumerate(selected_stops):
            stop_cost = float(s.fuel_cost.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            response_total_cost += stop_cost
            fuel_stops_response.append({
                'stop_number': i + 1,
                'station_name': s.station_name,
                'city': s.city,
                'state': s.state,
                'address': s.address or '',
                'mile_marker': round(s.distance_from_start, 1),
                'fuel_price_per_gallon': float(s.price_per_gallon),
                'gallons_to_buy': round(s.gallons_to_buy, 1),
                'fuel_cost': stop_cost,
                'detour_miles': round(
                    s.detour_miles if s.detour_miles is not None else 0.0, 1
                ),
                'cost_per_mile': round(
                    s.cost_per_mile if s.cost_per_mile is not None else 0.0, 3
                ),
            })
        response_total_cost = round(response_total_cost, 2)

        # Detect infeasible routes: no stations found, or stops don't cover full distance
        no_stations_available = (
            not selected_stops and selected_route.distance_miles > VEHICLE_MAX_RANGE
        )
        insufficient_coverage = False
        pv_id = _get_active_price_version()
        avg_price = _get_avg_price_for_version(pv_id)

        # Fuel accounting
        if selected_stops:
            last_stop_fuel_after = float(selected_stops[-1].fuel_after_refuel)
            last_stop_dist = selected_stops[-1].distance_from_start
            final_leg_miles = selected_route.distance_miles - last_stop_dist
            unclamped_remaining = last_stop_fuel_after - final_leg_miles / VEHICLE_MPG
            fuel_remaining_val = max(
                float(MIN_DESTINATION_RESERVE_GALLONS),
                unclamped_remaining
            )
            if unclamped_remaining < 0:
                insufficient_coverage = True
                logger.warning(
                    f"[{request_id}] Insufficient fuel coverage: "
                    f"last stop at {last_stop_dist:.0f}mi leaves {unclamped_remaining:.1f}gal "
                    f"for remaining {final_leg_miles:.0f}mi leg"
                )
        elif no_stations_available:
            fuel_remaining_val = 0.0
        else:
            fuel_remaining_val = max(
                float(MIN_DESTINATION_RESERVE_GALLONS),
                float(VEHICLE_TANK) - selected_route.distance_miles / VEHICLE_MPG
            )

        fuel_purchased_total = sum(
            round(s.gallons_to_buy, 1) for s in selected_stops
        )
        actual_fuel_consumed = (
            VEHICLE_TANK + fuel_purchased_total - fuel_remaining_val
        )

        # For infeasible routes (no stations or stops don't cover full distance),
        # report distance-based fuel consumption (not tank-constrained)
        # and show the deficit as negative remaining — the negative value
        # communicates "you'd run out X gallons before finishing."
        infeasible = no_stations_available or insufficient_coverage
        if infeasible:
            actual_fuel_consumed = selected_route.distance_miles / VEHICLE_MPG
            fuel_remaining_val = VEHICLE_TANK + fuel_purchased_total - actual_fuel_consumed

        # Determine if route is physically impossible (fuel deficit even with all stops)
        required_fuel_gallons = selected_route.distance_miles / VEHICLE_MPG
        available_fuel_gallons = VEHICLE_TANK + fuel_purchased_total
        remaining_fuel = available_fuel_gallons - required_fuel_gallons
        route_impossible = remaining_fuel < -0.1  # tolerance for floating-point edge cases

        if route_impossible:
            unreachable_cost = round(avg_price * available_fuel_gallons, 2)
            warning_msg = (
                'No fuel stations found in database for this route corridor. '
                'Route cannot be completed.'
            ) if no_stations_available else (
                'Insufficient station coverage for complete optimization. '
                'Route cannot be completed.'
            )
            response = {
                '_cache_hit': False,
                'route_feasible': False,
                'optimization_confidence': 'insufficient_data',
                'status': 'unreachable',
                'request_id': request_id,
                'optimization_time_ms': elapsed_ms,
                'request': {
                    'start': {
                        'city': start_city,
                        'state': start_state,
                        'formatted_address': start_formatted
                    },
                    'finish': {
                        'city': end_city,
                        'state': end_state,
                        'formatted_address': end_formatted
                    }
                },
                'selected_route': {
                    'route_id': selected_route.route_id,
                    'distance_miles': round(selected_route.distance_miles, 1),
                    'is_optimal': False,
                    'reason': 'Unable to complete route',
                    'estimated_total_fuel_consumption_gallons': round(available_fuel_gallons, 1),
                    'estimated_total_fuel_cost': unreachable_cost,
                    'fuel_stops_required': 0,
                    'warning': warning_msg,
                },
                'route_comparison': [
                    {
                        'route_id': r.route_id,
                        'distance_miles': round(r.distance_miles, 1),
                        'estimated_total_fuel_cost': unreachable_cost,
                        'fuel_stops_required': len(s),
                        'selected': r.route_id == selected_route.route_id
                    }
                    for r, s, c in route_optimizations
                ],
                'warning': warning_msg,
                'fuel_stops': [],
                'trip_summary': {
                    'total_distance_miles': round(selected_route.distance_miles, 1),
                    'required_fuel_gallons': round(required_fuel_gallons, 1),
                    'available_fuel_gallons': round(available_fuel_gallons, 1),
                    'missing_fuel_gallons': round(abs(remaining_fuel), 1),
                    'starting_fuel_gallons': VEHICLE_TANK,
                    'fuel_purchased_at_stops': round(fuel_purchased_total, 1),
                    'total_fuel_available': round(available_fuel_gallons, 1),
                    'total_fuel_consumed_gallons': round(available_fuel_gallons, 1),
                    'fuel_remaining_at_destination': 0.0,
                    'total_fuel_cost': unreachable_cost,
                    'average_price_per_gallon': round(avg_price, 2),
                    'total_fuel_stops': 0,
                }
            }
            set_cached_optimization(start_input, end_input, response, price_version=price_version_obj.id)
            logger.warning(
                f"[{request_id}] Route impossible: need {required_fuel_gallons:.1f}gal, "
                f"have {available_fuel_gallons:.1f}gal, "
                f"shortfall {abs(remaining_fuel):.1f}gal"
            )
            return response

        # Compute cost estimate — use distance-based for infeasible routes
        if infeasible:
            estimated_fuel_cost = round(
                (selected_route.distance_miles / VEHICLE_MPG) * avg_price, 2
            )
            logger.warning(
                f"[{request_id}] {'No stations' if no_stations_available else 'Insufficient coverage'}. "
                f"Estimated cost at ${avg_price:.2f}/gal: ${estimated_fuel_cost:.2f}"
            )
        else:
            estimated_fuel_cost = response_total_cost

        # Generate Google Maps navigation link
        route_map_link = _build_map_link(start_input, end_input, start_loc, end_loc)

        response = {
            '_cache_hit': False,
            'route_feasible': not infeasible,
            'optimization_confidence': (
                'high' if selected_stops and not insufficient_coverage
                else 'insufficient_data' if infeasible
                else 'estimated'
            ),
            'status': 'success',
            'request_id': request_id,
            'optimization_time_ms': elapsed_ms,
            'request': {
                'start': {
                    'city': start_city,
                    'state': start_state,
                    'formatted_address': start_formatted
                },
                'finish': {
                    'city': end_city,
                    'state': end_state,
                    'formatted_address': end_formatted
                }
            },
            'selected_route': {
                'route_id': selected_route.route_id,
                'distance_miles': round(selected_route.distance_miles, 1),
                'is_optimal': True,
                'reason': (
                    'Lowest total fuel cost'
                    if not infeasible
                    else 'Estimated (insufficient station coverage)'
                ),
                'estimated_total_fuel_consumption_gallons': round(actual_fuel_consumed, 1),
                'estimated_total_fuel_cost': (
                    estimated_fuel_cost if infeasible else response_total_cost
                ),
                'fuel_stops_required': len(selected_stops),
                'route_polyline': selected_route.polyline_encoded,
                'route_map_link': route_map_link,
                'warning': (
                    'No fuel stations found in database for this route corridor. '
                    'Fuel cost is estimated.'
                ) if no_stations_available else (
                    'Insufficient station coverage for complete optimization. '
                    'Fuel stops do not cover full route distance.'
                ) if insufficient_coverage else None,
            },
            'route_comparison': [
                {
                    'route_id': r.route_id,
                    'distance_miles': round(r.distance_miles, 1),
                    'estimated_total_fuel_cost': (
                        estimated_fuel_cost
                        if (infeasible and r.route_id == selected_route.route_id)
                        else response_total_cost
                        if (not infeasible and r.route_id == selected_route.route_id)
                        else round(c, 2)
                    ),
                    'fuel_stops_required': len(s),
                    'selected': r.route_id == selected_route.route_id
                }
                for r, s, c in route_optimizations
            ],
            'fuel_stops': fuel_stops_response,
            'trip_summary': {
                'total_distance_miles': round(selected_route.distance_miles, 1),
                'total_fuel_cost': (
                    estimated_fuel_cost if infeasible else response_total_cost
                ),
                'average_price_per_gallon': (
                    float(response_total_cost / fuel_purchased_total) if selected_stops
                    else (estimated_fuel_cost / (selected_route.distance_miles / VEHICLE_MPG)
                          if infeasible else 0)
                ),
                'total_fuel_stops': len(selected_stops),
                'starting_fuel_gallons': VEHICLE_TANK,
                'fuel_purchased_at_stops': round(fuel_purchased_total, 1),
                'total_fuel_available': round(VEHICLE_TANK + fuel_purchased_total, 1),
                'total_fuel_consumed_gallons': round(actual_fuel_consumed, 1),
                'fuel_remaining_at_destination': round(fuel_remaining_val, 1),
            }
        }

        # Cache result using unified cache service (use DB-fresh version, not potentially stale pv)
        cache_pv = price_version_obj.id if price_version_obj else pv
        set_cached_optimization(start_input, end_input, response, price_version=cache_pv)
        logger.info(f"[{request_id}] Optimization complete in {elapsed_ms}ms")

        return response

    @staticmethod
    def _validate_route_geometry(
        selected_stops, selected_route, request_id
    ) -> Tuple[bool, Optional[str]]:
        """Validate route geometry for forward progression."""
        fuel_stops_dict = [
            {
                'station': {
                    'name': s.station_name,
                    'latitude': float(s.latitude) if s.latitude else 0,
                    'longitude': float(s.longitude) if s.longitude else 0,
                },
                'distance_from_start_miles': float(s.distance_from_start),
                'gallons_to_buy': float(s.gallons_to_buy),
            }
            for s in selected_stops
        ]

        try:
            polyline_coords = decode_route_to_coordinates(
                selected_route.polyline_encoded
            ) if selected_route.polyline_encoded else []
        except Exception:
            polyline_coords = []

        is_valid, error_msg = RouteGeometryValidator.validate_stop_sequence(
            fuel_stops_dict, polyline_coords, selected_route.distance_miles
        )

        if not is_valid:
            return False, error_msg

        RouteGeometryValidator.validate_interstate_consistency(
            fuel_stops_dict, polyline_coords
        )
        return True, None

    @staticmethod
    def _resolve_location(
        location_input: str | Dict[str, float]
    ) -> Optional[Location]:
        """Resolve location from string address or lat/lng dict."""
        if isinstance(location_input, dict):
            lat = location_input.get('lat')
            if lat is None:
                lat = location_input.get('latitude')
            lon = location_input.get('lng')
            if lon is None:
                lon = location_input.get('longitude')
            return Location(latitude=lat, longitude=lon)
        return GeocodingService.geocode(location_input)
