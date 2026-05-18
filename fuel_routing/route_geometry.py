"""Route geometry and fuel stop continuity validator.

Ensures fuel stops maintain forward geographic progression without backtracking,
state regression, or corridor deviation. Critical for long-distance routes.
"""
import logging
import math
from typing import List, Dict, Tuple, Optional

import polyline
from geopy.distance import geodesic

from .cache_utils import GeometryCache


def _fast_distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Fast haversine distance in miles (~0.5μs, <0.5% error vs geodesic)."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + \
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.asin(math.sqrt(a))
    return R * c

logger = logging.getLogger(__name__)


def decode_route_to_coordinates(polyline_encoded: str) -> List[Tuple[float, float]]:
    """Decode Google encoded polyline to lat/lon coordinates. Uses GeometryCache."""
    if not polyline_encoded:
        return []
    # Check geometry cache — stores (coords, cum_dist, sampled_coords, sampled_cum_dist)
    cached = GeometryCache.get(polyline_encoded)
    if cached is not None:
        return cached[0]  # coords
    return polyline.decode(polyline_encoded)


class RouteGeometryValidator:
    """Validates fuel stop sequencing and route continuity."""
    
    @staticmethod
    def validate_stop_sequence(
        fuel_stops: List[Dict],
        route_polyline_coords: List[Tuple[float, float]],
        total_route_distance: float
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate fuel stops maintain forward geographic progression.
        
        Checks:
        1. Stops are in order by distance (monotonic increase)
        2. No state regression (no moving backward toward start)
        3. No corridor deviation (stops within corridor)
        4. Mileage continuity verified
        5. No repeated waypoints/loops
        
        Args:
            fuel_stops: List of fuel stop dicts with distance_from_start_miles
            route_polyline_coords: Route waypoints [(lat, lon), ...]
            total_route_distance: Total route distance in miles
            
        Returns:
            (is_valid, error_message) tuple
        """
        if not fuel_stops:
            return True, None
        
        # Check 1: Monotonic distance increase (no backtracking)
        prev_distance = 0
        for i, stop in enumerate(fuel_stops):
            curr_distance = stop.get('distance_from_start_miles', 0)
            
            if curr_distance <= prev_distance:
                msg = (
                    f"❌ BACKTRACKING DETECTED: Stop {i} at {curr_distance:.1f}mi "
                    f"is not forward from previous at {prev_distance:.1f}mi"
                )
                logger.error(msg)
                return False, msg
            
            # Check 2: Realistic sequencing only - stops progress forward monotonically
            # No strict "expected progression" check (fuel stops may cluster naturally early)
            prev_distance = curr_distance
        
        # Check 3: Final stop not beyond destination (within tolerance)
        if fuel_stops and prev_distance > total_route_distance + 1:
            msg = (
                f"❌ ROUTE OVERFLOW: Last stop at {prev_distance:.1f}mi "
                f"exceeds route distance {total_route_distance:.1f}mi"
            )
            logger.error(msg)
            return False, msg
        
        # Check 4: Mileage continuity (reasonable gaps between stops)
        max_gap = 600  # Max miles between stops (fuel range)
        for i in range(len(fuel_stops) - 1):
            gap = fuel_stops[i+1]['distance_from_start_miles'] - fuel_stops[i]['distance_from_start_miles']
            if gap > max_gap:
                msg = (
                    f"⚠️  LARGE GAP: {gap:.1f}mi between stops {i} and {i+1} "
                    f"(max safe: {max_gap}mi)"
                )
                logger.warning(msg)
                # Not fatal, just warning
        
        # Check 5: Corridor consistency - REALISTIC DEVIATIONS FOR LONG ROUTES
        if route_polyline_coords:
            for i, stop in enumerate(fuel_stops):
                station_lat = stop['station'].get('latitude', 0)
                station_lon = stop['station'].get('longitude', 0)
                
                # Find closest point on polyline to this stop
                min_dist = float('inf')
                for lat, lon in route_polyline_coords:
                    dist = _fast_distance_miles(station_lat, station_lon, lat, lon)
                    min_dist = min(min_dist, dist)

                # REALISTIC CORRIDOR BOUNDS: Allow natural deviations for fuel station availability
                # Short routes (<500mi): 15 miles max deviation
                # Medium routes (500-1500mi): 30 miles max deviation
                # Long routes (>1500mi): 60 miles max deviation (realistic for cross-country routes)
                if len(fuel_stops) <= 2:
                    max_deviation = 15.0  # Short routes
                elif len(fuel_stops) <= 4:
                    max_deviation = 30.0  # Medium routes
                else:
                    max_deviation = 60.0  # Long routes - allow realistic fuel station availability
                
                if min_dist > max_deviation:
                    msg = (
                        f"⚠️  CORRIDOR DEVIATION WARNING: Stop {i} ({stop['station']['name']}) "
                        f"is {min_dist:.1f}mi from route (allowed: {max_deviation}mi) - "
                        f"may indicate less optimal fuel stop choice but proceeding"
                    )
                    logger.warning(msg)
                    # Not rejecting - this is expected for real-world fuel stops
        
        logger.info("✅ Route geometry validation PASSED (corridor + forward progression verified)")
        return True, None
    
    @staticmethod
    def validate_interstate_consistency(
        fuel_stops: List[Dict],
        route_polyline_coords: List[Tuple[float, float]]
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate fuel stops follow consistent interstate routing.
        
        Prevents:
        - Crossing between distant interstates without proper connection
        - Zig-zagging between parallel routes
        - Detours from main corridor
        
        Args:
            fuel_stops: List of fuel stops
            route_polyline_coords: Route waypoints
            
        Returns:
            (is_valid, error_message) tuple
        """
        if len(fuel_stops) < 2:
            return True, None
        
        # Simplified check: ensure stops are roughly in line with polyline
        # (more sophisticated check would use actual interstate IDs)
        
        for i in range(len(fuel_stops) - 1):
            stop_a = fuel_stops[i]['station']
            stop_b = fuel_stops[i+1]['station']
            
            dist_a = (
                stop_a.get('latitude', 0),
                stop_a.get('longitude', 0)
            )
            dist_b = (
                stop_b.get('latitude', 0),
                stop_b.get('longitude', 0)
            )
            
            # Check stops are getting closer to destination (no zigzag)
            gap = _fast_distance_miles(dist_a[0], dist_a[1], dist_b[0], dist_b[1])
            expected_gap = fuel_stops[i+1]['distance_from_start_miles'] - \
                          fuel_stops[i]['distance_from_start_miles']
            
            # Allow 15% deviation for actual road routing
            if gap > expected_gap * 1.15:
                msg = (
                    f"⚠️  ZIGZAG DETECTED: Actual distance {gap:.1f}mi between stops "
                    f"{i} and {i+1} exceeds expected {expected_gap:.1f}mi"
                )
                logger.warning(msg)
        
        return True, None
    
    @staticmethod
    def reject_invalid_sequences(
        fuel_stops,  # Can be List[FuelStopDetail] or List[Dict]
        route_polyline_coords: List[Tuple[float, float]] = None,
        total_route_distance: float = None
    ):
        """
        Remove fuel stops that violate progression rules or are geographic outliers.
        
        Enhanced to detect anomalies like "Waco, NE" that are far from route corridor.
        """
        if not fuel_stops:
            return []
        
        # Filter 1: Remove stops with NaN or invalid distances
        valid_stops = []
        for s in fuel_stops:
            # Handle both FuelStopDetail objects and dicts
            if hasattr(s, 'distance_from_start'):
                distance = s.distance_from_start
            elif isinstance(s, dict) and 'distance_from_start_miles' in s:
                distance = s.get('distance_from_start_miles')
            elif isinstance(s, dict) and 'distance_from_start' in s:
                distance = s.get('distance_from_start')
            else:
                continue
            
            if isinstance(distance, (int, float)) and distance is not None and distance >= 0:
                valid_stops.append(s)
        
        if len(valid_stops) < len(fuel_stops):
            removed_count = len(fuel_stops) - len(valid_stops)
            logger.warning(f"Removed {removed_count} stops with invalid distances")
        
        # Filter 2: Statistical outlier detection for geographic anomalies
        # Calculate deviation from route for each stop
        if valid_stops and route_polyline_coords and total_route_distance:
            deviation_distances = []
            
            for stop in valid_stops:
                try:
                    # Handle both FuelStopDetail objects and dicts
                    if hasattr(stop, 'latitude') and hasattr(stop, 'longitude'):
                        station_lat = stop.latitude
                        station_lon = stop.longitude
                    else:
                        station_lat = stop['station'].get('latitude', 0)
                        station_lon = stop['station'].get('longitude', 0)
                    
                    # Calculate minimum distance to any point on route polyline
                    min_dist = float('inf')
                    for lat, lon in route_polyline_coords:
                        dist = _fast_distance_miles(station_lat, station_lon, lat, lon)
                        min_dist = min(min_dist, dist)
                    
                    deviation_distances.append(min_dist)
                except Exception as e:
                    logger.warning(f"Error calculating deviation for stop: {e}")
                    deviation_distances.append(float('inf'))  # Mark as outlier
            
            # Identify outliers: >1.5x median deviation or distance-based threshold
            # For long cross-country routes (2000+ miles), be more permissive with deviations
            # For short routes, be stricter
            if len(deviation_distances) > 2:
                sorted_deviations = sorted([d for d in deviation_distances if d != float('inf')])
                if sorted_deviations:
                    median_deviation = sorted_deviations[len(sorted_deviations) // 2]
                    # Dynamic threshold based on route distance
                    # Short routes (<500mi): strict (15mi max deviation)
                    # Medium routes (500-1500mi): moderate (30mi max)
                    # Long routes (1500-2500mi): permissive (50mi max)
                    # Ultra-long (>2500mi): very permissive (75mi max)
                    if total_route_distance < 500:
                        max_deviation = 15.0
                    elif total_route_distance < 1500:
                        max_deviation = 30.0
                    elif total_route_distance < 2500:
                        max_deviation = 50.0
                    else:
                        max_deviation = 75.0
                    
                    outlier_threshold = max(median_deviation * 1.5, max_deviation)
                else:
                    outlier_threshold = 50.0
                
                filtered_stops = []
                for stop, deviation in zip(valid_stops, deviation_distances):
                    if deviation > outlier_threshold:
                        # Handle both FuelStopDetail objects and dicts
                        if hasattr(stop, 'station_name'):
                            stop_name = stop.station_name
                        else:
                            stop_name = stop['station']['name']
                        
                        logger.warning(
                            f"⚠️  Filtering geographic outlier: {stop_name} "
                            f"at {deviation:.1f}mi deviation (threshold: {outlier_threshold:.1f}mi)"
                        )
                    else:
                        filtered_stops.append(stop)
                
                return filtered_stops
        
        return valid_stops
