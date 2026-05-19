"""Fuel optimization engine implementing Greedy + Lookahead algorithm."""
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Avg

import polyline

from .constants import (
    LOOKAHEAD_MILES, MAX_DETOUR_MILES, VEHICLE_MPG, VEHICLE_RESERVE_MILES,
    VEHICLE_TANK, VEHICLE_MAX_RANGE
)
from .geocoding import Location, _fast_distance_miles
from .models import FuelPrice, PriceVersion
from .route_geometry import decode_route_to_coordinates
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
    """Greedy + Lookahead fuel stop optimization algorithm."""

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
        sampled_coords: List[Tuple[float, float]],
        sampled_cum_dist: List[float]
    ) -> Tuple[float, float]:
        """Snap a fuel station to the nearest point along the route polyline."""
        min_dist = float('inf')
        best_idx = 0

        for i, (lat, lon) in enumerate(sampled_coords):
            d = _fast_distance_miles(lat, lon, station_lat, station_lon)
            if d < min_dist:
                min_dist = d
                best_idx = i

        return sampled_cum_dist[best_idx], min_dist

    @staticmethod
    def calculate_fuel_stops(
        route: RouteAlternative,
        available_stations: List[Dict[str, Any]],
        start_location: Location,
        end_location: Location
    ) -> List[FuelStopDetail]:
        """Calculate optimal fuel stops using Greedy + Route-Aware Lookahead algorithm."""
        logger.info(f"Optimizing fuel stops for {route.route_id} ({route.distance_miles:.1f} miles)")

        # Precompute route distances
        all_coords, cum_distances, sampled_coords, sampled_cum_dist = \
            FuelOptimizer.precompute_route_distances(route.polyline_encoded) \
            if route.polyline_encoded else ([], [], [], [])

        has_route_data = len(sampled_coords) > 0

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
                    sampled_coords,
                    sampled_cum_dist
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

        stops = []
        visited_stations = set()

        current_fuel = VEHICLE_TANK
        current_position = 0.0

        total_fuel_purchased = 0.0

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

                max_fuel_for_travel = current_fuel - (VEHICLE_RESERVE_MILES / VEHICLE_MPG)
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

            # Greedy + Lookahead selection (sorted by DISTANCE, not price)
            candidates.sort(key=lambda x: x['distance_from_start'])

            selected = None
            selected_strategy = None
            target_cheaper = None

            for candidate in candidates:
                station_distance = candidate['distance_from_start']
                lookahead_limit = station_distance + LOOKAHEAD_MILES

                cheaper_ahead = None
                for other in candidates:
                    if other['distance_from_start'] > station_distance and \
                       other['distance_from_start'] <= lookahead_limit:
                        if other['price'] < candidate['price']:
                            if cheaper_ahead is None or other['price'] < cheaper_ahead['price']:
                                cheaper_ahead = other

                if cheaper_ahead:
                    if cheaper_ahead['fuel_at_arrival'] >= 0:
                        logger.debug(
                            f"Skipping {candidate['station']['name']} (@{candidate['price']:.3f}) - "
                            f"can reach cheaper {cheaper_ahead['station']['name']} (@{cheaper_ahead['price']:.3f}) directly"
                        )
                        continue

                    selected = candidate
                    selected_strategy = 'partial'
                    target_cheaper = cheaper_ahead
                    break
                else:
                    selected = candidate
                    selected_strategy = 'fill'
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

            if selected_strategy == 'partial' and target_cheaper:
                dist_to_cheaper = target_cheaper['distance_from_start'] - selected['distance_from_start']
                fuel_needed_to_cheaper = (dist_to_cheaper + 20.0) / VEHICLE_MPG
                fuel_to_buy = max(0.0, min(
                    fuel_needed_to_cheaper - fuel_at_arrival,
                    float(VEHICLE_TANK) - fuel_at_arrival
                ))
            else:
                fuel_to_buy = max(0.0, float(VEHICLE_TANK) - fuel_at_arrival)

            # Skip micro-purchases (< 5 gallons)
            if fuel_to_buy < 5.0:
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
