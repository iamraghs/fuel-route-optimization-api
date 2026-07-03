"""Fuel optimization engine implementing Range-Aware Greedy algorithm."""
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Tuple

import math
from django.db.models import Avg

import polyline

from .constants import (
    MAX_DETOUR_MILES, VEHICLE_MPG, VEHICLE_RESERVE_MILES,
    VEHICLE_TANK, VEHICLE_MAX_RANGE
)
from .geocoding import Location, _fast_distance_miles
from .models import FuelPrice
from .routing import RouteAlternative
from .cache_utils import GeometryCache

logger = logging.getLogger(__name__)


@dataclass
class FuelStopDetail:
    """Fuel stop along route with all optimization details."""
    opis_id: int
    station_name: str
    city: str
    state: str
    address: str
    latitude: float
    longitude: float
    price_per_gallon: Decimal
    distance_from_start: float
    mile_marker: float
    gallons_to_buy: float
    fuel_cost: Decimal
    fuel_remaining_at_arrival: float
    fuel_after_refuel: float
    range_remaining_at_arrival: float
    remaining_range_after_fill: float

    detour_miles: float = 0.0
    cost_per_mile: float = field(default=0.0)


# In-process cache: {price_version_id: avg_price}
_avg_price_cache: Dict[int, float] = {}
_AVG_PRICE_CACHE_MAX = 10  # prevent unbounded growth (only 1-2 expected)
_RESERVE_GALLONS = VEHICLE_RESERVE_MILES / VEHICLE_MPG  # 50mi / 10mpg = 5 gallons


def get_cached_avg_price(price_version_id: int, force: bool = False) -> float:
    """Get average fuel price, cached in-process per version ID."""
    if force:
        _avg_price_cache.pop(price_version_id, None)
    if price_version_id not in _avg_price_cache:
        try:
            avg = FuelPrice.objects.filter(
                version_id=price_version_id
            ).aggregate(avg=Avg('price_per_gallon'))['avg']
            _avg_price_cache[price_version_id] = float(avg) if avg else 3.45
        except Exception:
            _avg_price_cache[price_version_id] = 3.45
    # Trim if over max size (evict oldest — dict preserves insertion order in 3.7+)
    while len(_avg_price_cache) > _AVG_PRICE_CACHE_MAX:
        _avg_price_cache.pop(next(iter(_avg_price_cache)))
    return _avg_price_cache[price_version_id]


class FuelOptimizer:
    """Range-Aware Greedy fuel stop optimization algorithm with full-range lookahead."""

    @staticmethod
    def precompute_route_distances(
        polyline_encoded: str
    ) -> Tuple[List[Tuple[float, float]], List[float], List[Tuple[float, float]], List[float]]:
        """Decode polyline and precompute cumulative distances at each waypoint.

        Uses in-process GeometryCache to avoid repeated decode + cum distance computation.
        """
        # Check geometry cache first
        cached = GeometryCache.get(polyline_encoded)
        if cached is not None:
            return cached

        coords = polyline.decode(polyline_encoded)
        if not coords:
            GeometryCache.set(polyline_encoded, [], [], [], [])
            return [], [], [], []

        cum_dist = [0.0]
        for i in range(1, len(coords)):
            d = _fast_distance_miles(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
            cum_dist.append(cum_dist[-1] + d)

        sample_rate = max(1, len(coords) // 200)
        sampled_coords = coords[::sample_rate]
        sampled_cum_dist = cum_dist[::sample_rate]

        if sampled_coords[-1] != coords[-1]:
            sampled_coords.append(coords[-1])
            sampled_cum_dist.append(cum_dist[-1])

        # Store in cache
        GeometryCache.set(polyline_encoded, coords, cum_dist, sampled_coords, sampled_cum_dist)

        return coords, cum_dist, sampled_coords, sampled_cum_dist

    @staticmethod
    def snap_station_to_route(
        station_lat: float,
        station_lon: float,
        all_coords: List[Tuple[float, float]],
        all_cum_dist: List[float]
    ) -> Tuple[float, float]:
        """Snap a fuel station to the nearest point along the route polyline.

        Uses perpendicular cross-track distance to line segments, not nearest-point
        approximation. This correctly handles sparse polyline coordinates where
        stations between coordinate points were previously falsely rejected.

        Pre-computes segment bearings once per call for performance.
        """
        n = len(all_coords)
        if n == 0:
            return 0.0, 0.0
        if n == 1:
            d = _fast_distance_miles(all_coords[0][0], all_coords[0][1], station_lat, station_lon)
            return all_cum_dist[0], d

        R = 3958.8

        # Pre-compute segment bearings and lengths (done once per call, shared across stations)
        seg_bearings = []
        seg_lengths_mi = []
        for i in range(n - 1):
            lat1, lon1 = all_coords[i]
            lat2, lon2 = all_coords[i + 1]
            seg_len = _fast_distance_miles(lat1, lon1, lat2, lon2)
            seg_lengths_mi.append(seg_len)

            y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
            x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) -
                 math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) *
                 math.cos(math.radians(lon2 - lon1)))
            seg_bearings.append(math.atan2(y, x))

        lat_r = math.radians(station_lat)
        lon_r = math.radians(station_lon)

        min_detour = float('inf')
        best_cum_dist = 0.0

        for i in range(n - 1):
            lat1, lon1 = all_coords[i]
            lat1_r = math.radians(lat1)
            lon1_r = math.radians(lon1)
            seg_len = seg_lengths_mi[i]

            # For very short segments (<0.1mi), use endpoint distance directly
            # to avoid bearing instability at close coordinates
            if seg_len < 0.1:
                d1 = _fast_distance_miles(lat1, lon1, station_lat, station_lon)
                if d1 < min_detour:
                    min_detour = d1
                    best_cum_dist = all_cum_dist[i]
                    if min_detour < 1e-10:
                        break
                d2 = _fast_distance_miles(lat1, lon1, station_lat, station_lon)
                if d2 < min_detour:
                    min_detour = d2
                    best_cum_dist = all_cum_dist[i + 1]
                    if min_detour < 1e-10:
                        break
                continue

            # Haversine distance from segment start to station
            dlat = lat_r - lat1_r
            dlon = lon_r - lon1_r
            a = (math.sin(dlat / 2.0) ** 2 +
                 math.cos(lat1_r) * math.cos(lat_r) * math.sin(dlon / 2.0) ** 2)
            d13 = R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

            # Bearing from segment start to station
            y = math.sin(lon_r - lon1_r) * math.cos(lat_r)
            x = (math.cos(lat1_r) * math.sin(lat_r) -
                 math.sin(lat1_r) * math.cos(lat_r) * math.cos(lon_r - lon1_r))
            theta13 = math.atan2(y, x)

            # Cross-track distance (perpendicular)
            sin_d13_R = math.sin(d13 / R)
            sin_theta_diff = math.sin(theta13 - seg_bearings[i])
            dxt = math.asin(max(-1.0, min(1.0, sin_d13_R * sin_theta_diff))) * R

            # Along-track distance (projection onto segment)
            cos_d13_R = math.cos(d13 / R)
            cos_dxt_R = math.cos(dxt / R)
            if abs(cos_dxt_R) > 1e-12:
                along_arg = max(-1.0, min(1.0, cos_d13_R / cos_dxt_R))
                dt = math.acos(along_arg) * R
            else:
                dt = d13

            # Check if projection falls within the segment
            if dt < 0.0:
                # Projection before segment start — use start-point distance
                d = _fast_distance_miles(lat1, lon1, station_lat, station_lon)
                cum = all_cum_dist[i]
            elif dt > seg_len:
                # Projection after segment end — use end-point distance
                lat2, lon2 = all_coords[i + 1]
                d = _fast_distance_miles(lat2, lon2, station_lat, station_lon)
                cum = all_cum_dist[i + 1]
            else:
                # Station projects onto the segment — use perpendicular distance
                d = abs(dxt)
                cum = all_cum_dist[i] + dt

            if d < min_detour:
                min_detour = d
                best_cum_dist = cum
                if min_detour < 1e-10:
                    break

        return best_cum_dist, min_detour

    @staticmethod
    def calculate_fuel_stops(
        route: RouteAlternative,
        available_stations: List[Dict[str, Any]],
        start_location: Location,
        end_location: Location
    ) -> List[FuelStopDetail]:
        """Calculate optimal fuel stops using Range-Aware Greedy algorithm."""
        logger.info(f"Optimizing fuel stops for {route.route_id} ({route.distance_miles:.1f} miles)")

        # Precompute route distances
        all_coords, cum_distances, _, _ = \
            FuelOptimizer.precompute_route_distances(route.polyline_encoded) \
            if route.polyline_encoded else ([], [], [], [])

        has_route_data = len(all_coords) > 0

        # Build snapped distance lookup
        station_route_data = {}
        for station in available_stations:
            if '_snapped_distance' in station:
                snapped_dist = station['_snapped_distance']
                detour = station.get('_detour_miles', 0.0)
            elif has_route_data:
                snapped_dist, detour = FuelOptimizer.snap_station_to_route(
                    float(station['latitude']),
                    float(station['longitude']),
                    all_coords,
                    cum_distances
                )
            else:
                snapped_dist = _fast_distance_miles(
                    start_location.latitude, start_location.longitude,
                    float(station['latitude']), float(station['longitude'])
                )
                detour = 0.0

            station_route_data[station['opis_id']] = {
                'snapped_distance': snapped_dist,
                'detour_miles': detour,
            }

        # Full station index for range-aware cheaper-ahead lookups.
        # Unlike `candidates` (filtered to current-position reachable),
        # this includes ALL stations that become reachable after filling up.
        _station_index = {}
        for station in available_stations:
            oid = station['opis_id']
            sr = station_route_data.get(oid, {})
            _station_index[oid] = {
                'distance': sr.get('snapped_distance', 0.0),
                'price': Decimal(str(station['price_per_gallon'])),
                'name': station.get('name', ''),
            }

        stops = []
        visited_stations = set()

        current_fuel = VEHICLE_TANK
        current_position = 0.0

        total_fuel_purchased = 0.0

        # Pre-compute loop-invariant range values
        full_tank_range = float(VEHICLE_TANK) * VEHICLE_MPG
        full_tank_effective_range = full_tank_range - VEHICLE_RESERVE_MILES

        iteration = 0
        estimated_stops_needed = max(1, int(route.distance_miles / VEHICLE_MAX_RANGE) + 2)
        max_iterations = max(100, min(200, estimated_stops_needed * 20))
        logger.info(
            f"Route {route.distance_miles:.1f}mi: {len(available_stations)} stations, "
            f"starting with {current_fuel:.0f}gal full tank, "
            f"estimated {estimated_stops_needed} stops, {max_iterations} iteration limit"
        )

        while current_position < route.distance_miles and iteration < max_iterations:
            iteration += 1
            remaining_distance = route.distance_miles - current_position
            remaining_range = current_fuel * VEHICLE_MPG

            if remaining_distance <= remaining_range:
                logger.debug(
                    f"Destination reachable: {remaining_distance:.1f}mi remaining <= "
                    f"{remaining_range:.1f}mi range"
                )
                break

            # Build candidate list with route-aware distances
            candidates = []
            for station in available_stations:
                opis_id = station['opis_id']
                if opis_id in visited_stations:
                    continue

                route_info = station_route_data[opis_id]
                snapped_distance = route_info['snapped_distance']
                detour_miles = route_info['detour_miles']

                if snapped_distance <= current_position:
                    continue

                distance_along_route = snapped_distance - current_position

                if detour_miles > distance_along_route:
                    continue

                if detour_miles > MAX_DETOUR_MILES:
                    continue

                effective_distance = distance_along_route + 2.0 * detour_miles

                fuel_needed_to_reach = effective_distance / VEHICLE_MPG

                max_fuel_for_travel = current_fuel - _RESERVE_GALLONS
                if fuel_needed_to_reach > max_fuel_for_travel:
                    continue

                fuel_at_arrival = current_fuel - fuel_needed_to_reach
                price = Decimal(str(station['price_per_gallon']))

                candidates.append({
                    'station': station,
                    'distance_from_start': snapped_distance,
                    'detour_miles': detour_miles,
                    'price': price,
                    'fuel_at_arrival': fuel_at_arrival,
                    'fuel_needed': fuel_needed_to_reach,
                })

            if not candidates:
                logger.warning(
                    f"No reachable unvisited stations at position {current_position:.1f}mi. "
                    f"Visited: {len(visited_stations)}, Remaining: {remaining_distance:.1f}mi, "
                    f"Fuel: {current_fuel:.1f}gal"
                )
                break

            # Range-Aware Greedy selection
            candidates.sort(key=lambda x: x['distance_from_start'])

            selected = None
            selected_strategy = None
            target_cheaper = None

            # Effective range from current position (changes with each stop)
            current_effective_range = current_fuel * VEHICLE_MPG - VEHICLE_RESERVE_MILES

            for candidate in candidates:
                station_distance = candidate['distance_from_start']
                fuel_at_arrival = candidate['fuel_at_arrival']
                price = candidate['price']

                # --- CASE 1: Destination reachable from here after filling ---
                if route.distance_miles - station_distance <= full_tank_range:
                    # Check if a cheaper station is reachable from current position
                    # before committing — if so, skip to it for a better price
                    cheaper_before_dest = any(
                        other['price'] < price
                        for other in candidates
                        if other['distance_from_start'] > station_distance
                        and other['distance_from_start'] - current_position <= current_effective_range
                    )
                    if cheaper_before_dest:
                        logger.debug(
                            f"Skipping {candidate['station']['name']} (@{price:.3f}) for last stop - "
                            f"cheaper station ahead reachable directly"
                        )
                        continue

                    selected = candidate
                    selected_strategy = 'to_destination'
                    logger.debug(
                        f"Last stop at {candidate['station']['name']} (@{price:.3f}) - "
                        f"destination reachable with fill-up"
                    )
                    break

                # --- Look for cheaper stations reachable from here ---
                cheaper_ahead = None
                for oid, info in _station_index.items():
                    if oid in visited_stations:
                        continue
                    if info['distance'] <= station_distance:
                        continue
                    if info['distance'] > station_distance + full_tank_effective_range:
                        continue
                    if info['price'] < price:
                        if cheaper_ahead is None or info['price'] < cheaper_ahead['price']:
                            cheaper_ahead = {
                                'distance_from_start': info['distance'],
                                'price': info['price'],
                                'station': {'name': info['name']},
                                'opis_id': oid,
                            }

                if cheaper_ahead:
                    # Can we skip this station and reach the cheaper one directly?
                    if cheaper_ahead['distance_from_start'] - current_position <= current_effective_range:
                        logger.debug(
                            f"Skipping {candidate['station']['name']} (@{price:.3f}) - "
                            f"can reach cheaper {cheaper_ahead['station']['name']} "
                            f"(@{cheaper_ahead['price']:.3f}) directly"
                        )
                        continue

                    # --- CASE 2: Partial fill to reach cheaper station ---
                    selected = candidate
                    selected_strategy = 'partial'
                    target_cheaper = cheaper_ahead
                    logger.debug(
                        f"Partial fill at {candidate['station']['name']} (@{price:.3f}) - "
                        f"targeting cheaper {cheaper_ahead['station']['name']} "
                        f"(@{cheaper_ahead['price']:.3f})"
                    )
                    break
                else:
                    # --- CASE 3: No cheaper ahead — fill for max range flexibility ---
                    selected = candidate
                    selected_strategy = 'fill'
                    logger.debug(
                        f"Fill at {candidate['station']['name']} (@{price:.3f}) - "
                        f"no cheaper station within {full_tank_effective_range:.0f}mi range"
                    )
                    break

            if selected is None:
                if candidates:
                    selected = candidates[0]
                    selected_strategy = 'fill'
                    logger.warning("No strategic candidate, using closest station as fallback")
                else:
                    break

            # Calculate refuel amount based on strategy
            fuel_at_arrival = selected['fuel_at_arrival']

            if selected_strategy == 'to_destination':
                dest_distance = route.distance_miles - selected['distance_from_start']
                fuel_needed = (dest_distance + VEHICLE_RESERVE_MILES) / VEHICLE_MPG
                fuel_to_buy = max(0.0, min(
                    fuel_needed - fuel_at_arrival,
                    float(VEHICLE_TANK) - fuel_at_arrival
                ))
            elif selected_strategy == 'partial' and target_cheaper:
                dist_to_cheaper = target_cheaper['distance_from_start'] - selected['distance_from_start']
                fuel_needed_to_cheaper = (dist_to_cheaper + 20.0 + VEHICLE_RESERVE_MILES) / VEHICLE_MPG
                fuel_to_buy = max(0.0, min(
                    fuel_needed_to_cheaper - fuel_at_arrival,
                    float(VEHICLE_TANK) - fuel_at_arrival
                ))
            else:
                fuel_to_buy = max(0.0, float(VEHICLE_TANK) - fuel_at_arrival)

            # Skip micro-purchases (< 5 gallons)
            if fuel_to_buy < 5.0:
                # If skipping would strand us (no other reachable stations ahead),
                # override to fill instead — making progress always beats no progress
                fill_amount = max(0.0, float(VEHICLE_TANK) - fuel_at_arrival)
                has_other_stations = any(
                    c['station']['opis_id'] != selected['station']['opis_id']
                    for c in candidates
                )
                if not has_other_stations and fill_amount >= 5.0:
                    fuel_to_buy = fill_amount
                    selected_strategy = 'fill'
                    logger.debug(
                        f"Override to fill at {selected['station']['name']}: "
                        f"micro-refuel ({fill_amount:.1f}gal) would strand route"
                    )
                else:
                    logger.debug(
                        f"Skip micro-refuel at {selected['station']['name']}: "
                        f"would only buy {fuel_to_buy:.1f}gal (< 5gal minimum)"
                    )
                    visited_stations.add(selected['station']['opis_id'])
                    continue

            # Skip stops too close after a fill-up
            distance_from_last = selected['distance_from_start'] - current_position
            if distance_from_last < 80 and current_fuel > VEHICLE_TANK * 0.6:
                logger.debug(
                    f"Skip tight stop at {selected['station']['name']}: "
                    f"only {distance_from_last:.0f}mi from last stop with {current_fuel:.1f}gal remaining"
                )
                visited_stations.add(selected['station']['opis_id'])
                continue

            visited_stations.add(selected['station']['opis_id'])

            # Precise cost calculation
            fuel_to_buy_rounded = Decimal(str(round(fuel_to_buy, 1)))
            price_decimal = selected['price']
            cost_decimal = price_decimal * fuel_to_buy_rounded

            fuel_after_refuel = fuel_at_arrival + fuel_to_buy
            detour_miles = selected.get('detour_miles', 0.0) or 0.0

            stop = FuelStopDetail(
                opis_id=selected['station']['opis_id'],
                station_name=selected['station']['name'],
                city=selected['station']['city'],
                state=selected['station']['state'],
                address=selected['station'].get('address', ''),
                latitude=float(selected['station']['latitude']),
                longitude=float(selected['station']['longitude']),
                price_per_gallon=price_decimal,
                distance_from_start=selected['distance_from_start'],
                mile_marker=selected['distance_from_start'],
                gallons_to_buy=fuel_to_buy,
                fuel_cost=cost_decimal,
                fuel_remaining_at_arrival=fuel_at_arrival,
                fuel_after_refuel=fuel_after_refuel,
                range_remaining_at_arrival=fuel_at_arrival * VEHICLE_MPG,
                remaining_range_after_fill=fuel_after_refuel * VEHICLE_MPG,
                detour_miles=detour_miles,
                cost_per_mile=0.0,
            )

            stops.append(stop)

            old_position = current_position
            current_fuel = fuel_after_refuel
            total_fuel_purchased += fuel_to_buy
            current_position = selected['distance_from_start']

            if current_position <= old_position:
                logger.error(f"Route progression error: {old_position:.1f} -> {current_position:.1f}")
                if stops:
                    stops.pop()
                continue

        # Post-process cost_per_mile
        for i, stop in enumerate(stops):
            if i < len(stops) - 1:
                next_dist = stops[i + 1].distance_from_start
            else:
                next_dist = route.distance_miles

            segment_distance = next_dist - stop.distance_from_start
            if segment_distance > 0:
                stop.cost_per_mile = round(float(stop.fuel_cost) / segment_distance, 3)
            else:
                stop.cost_per_mile = 0.0

        # Final validation
        if iteration >= max_iterations:
            logger.warning(f"Hit iteration limit ({max_iterations}) - possible infinite loop")

        total_fuel_needed = route.distance_miles / VEHICLE_MPG
        fuel_purchased_at_stops = sum(round(s.gallons_to_buy, 1) for s in stops)
        total_fuel_available = VEHICLE_TANK + fuel_purchased_at_stops

        logger.info(
            f"Fuel state: start={VEHICLE_TANK:.1f}gal, "
            f"purchased={fuel_purchased_at_stops:.1f}gal, "
            f"available={total_fuel_available:.1f}gal, "
            f"needed={total_fuel_needed:.1f}gal"
        )

        if total_fuel_available < total_fuel_needed:
            shortage = total_fuel_needed - total_fuel_available
            logger.error(
                f"Insufficient fuel plan for {route.route_id}! "
                f"Need {total_fuel_needed:.1f}gal, have {total_fuel_available:.1f}gal "
                f"(shortfall: {shortage:.1f}gal)"
            )

        # Filter zero-fuel stops
        stops = [s for s in stops if s.gallons_to_buy > 0.01]

        # Recalculate total cost
        total_cost_decimal = Decimal('0')
        for s in stops:
            gallons_rounded = Decimal(str(round(s.gallons_to_buy, 1)))
            price_decimal = s.price_per_gallon if isinstance(s.price_per_gallon, Decimal) else Decimal(str(s.price_per_gallon))
            stop_cost = price_decimal * gallons_rounded
            total_cost_decimal += stop_cost

        total_cost = float(total_cost_decimal)

        logger.info(
            f"Calculated {len(stops)} fuel stops, total cost: ${total_cost:.2f}, "
            f"fuel purchased: {fuel_purchased_at_stops:.1f}gal"
        )
        return stops
