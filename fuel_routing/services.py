"""
Core business logic services for fuel routing optimization.

Services:
- RouteService: Google Routes API integration with caching
- FuelStopOptimizer: Multi-factor fuel stop selection
- CacheManager: Redis + DB cache with versioning
- GeoService: PostGIS geospatial queries
"""

import logging
import json
import hashlib
from typing import Dict, List, Tuple, Optional
from decimal import Decimal
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict

import requests
from django.core.cache import cache
from django.conf import settings
from django.db.models import Q, F
from django.utils import timezone

from .models import TruckStop, RouteCache, FuelStopCache

logger = logging.getLogger(__name__)


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class FuelStop:
    """Represents a fuel stop on the route."""
    opis_id: int
    name: str
    city: str
    state: str
    latitude: float
    longitude: float
    price_per_gallon: float
    distance_from_start: float
    distance_from_end: float
    gallons_needed: float
    cost: float


@dataclass
class RouteResult:
    """Complete route optimization result."""
    start_location: str
    end_location: str
    total_distance_miles: float
    total_fuel_cost: Decimal
    fuel_stops: List[FuelStop]
    route_polyline: Optional[str]
    timestamp: datetime
    cached: bool


# ============================================================================
# CACHE MANAGER (REDIS + DATABASE VERSIONING)
# ============================================================================

class CacheManager:
    """
    Advanced caching with Redis + PostgreSQL versioning.
    
    Strategy:
    1. Check Redis cache first (fast, in-memory)
    2. If miss, check database cache (persistent)
    3. If miss, compute and cache both
    4. Handle price updates by versioning
    """
    
    ROUTE_CACHE_KEY_PREFIX = "route:"
    FUEL_STOPS_CACHE_KEY_PREFIX = "fuel_stops:"
    PRICE_VERSION_KEY = "price_version"
    CACHE_TTL = 3600  # 1 hour
    
    @classmethod
    def get_route_cache_key(cls, start: str, end: str, version: int = 0) -> str:
        """Generate deterministic cache key for route."""
        route_str = f"{start.lower().strip()}|{end.lower().strip()}|v{version}"
        key_hash = hashlib.md5(route_str.encode()).hexdigest()[:16]
        return f"{cls.ROUTE_CACHE_KEY_PREFIX}{key_hash}"
    
    @classmethod
    def get_fuel_stops_cache_key(cls, route_id: int, version: int = 0) -> str:
        """Generate cache key for fuel stops."""
        return f"{cls.FUEL_STOPS_CACHE_KEY_PREFIX}{route_id}:v{version}"
    
    @classmethod
    def get_price_version(cls) -> int:
        """Get current price version (increments on fuel price updates)."""
        version = cache.get(cls.PRICE_VERSION_KEY, 0)
        return int(version)
    
    @classmethod
    def increment_price_version(cls):
        """Increment version when fuel prices change (invalidates all caches)."""
        current = cls.get_price_version()
        cache.set(cls.PRICE_VERSION_KEY, current + 1, None)
        logger.info(f"Price version incremented to {current + 1}")
    
    @classmethod
    def get_route_result(cls, start: str, end: str) -> Optional[RouteResult]:
        """
        Get cached route result from Redis → Database → None.
        
        Process:
        1. Check Redis (fast)
        2. Check database (persistent)
        3. Return None if not found
        """
        price_version = cls.get_price_version()
        cache_key = cls.get_route_cache_key(start, end, price_version)
        
        # Step 1: Check Redis
        cached_data = cache.get(cache_key)
        if cached_data:
            logger.info(f"Cache hit (Redis): {start} → {end}")
            return cls._deserialize_route_result(cached_data)
        
        # Step 2: Check database
        try:
            db_cache = RouteCache.objects.filter(
                cache_key=cache_key,
                expires_at__gt=timezone.now()
            ).first()
            
            if db_cache:
                logger.info(f"Cache hit (Database): {start} → {end}")
                # Populate Redis from database
                cache.set(cache_key, db_cache.result_data, cls.CACHE_TTL)
                return cls._deserialize_route_result(db_cache.result_data)
        except Exception as e:
            logger.warning(f"Database cache lookup failed: {e}")
        
        return None
    
    @classmethod
    def set_route_result(cls, start: str, end: str, result: RouteResult):
        """
        Cache route result in Redis + Database.
        
        Process:
        1. Serialize result
        2. Store in Redis (fast)
        3. Store in database (persistent)
        """
        price_version = cls.get_price_version()
        cache_key = cls.get_route_cache_key(start, end, price_version)
        
        serialized = cls._serialize_route_result(result)
        
        # Store in Redis
        cache.set(cache_key, serialized, cls.CACHE_TTL)
        
        # Store in database
        try:
            RouteCache.objects.update_or_create(
                cache_key=cache_key,
                defaults={
                    'start_location': start,
                    'end_location': end,
                    'result_data': serialized,
                    'expires_at': timezone.now() + timedelta(seconds=cls.CACHE_TTL)
                }
            )
        except Exception as e:
            logger.warning(f"Failed to cache in database: {e}")
    
    @staticmethod
    def _serialize_route_result(result: RouteResult) -> str:
        """Convert RouteResult to JSON string."""
        data = {
            'start_location': result.start_location,
            'end_location': result.end_location,
            'total_distance_miles': result.total_distance_miles,
            'total_fuel_cost': str(result.total_fuel_cost),
            'fuel_stops': [asdict(stop) for stop in result.fuel_stops],
            'route_polyline': result.route_polyline,
            'timestamp': result.timestamp.isoformat(),
        }
        return json.dumps(data)
    
    @staticmethod
    def _deserialize_route_result(data: str) -> RouteResult:
        """Convert JSON string back to RouteResult."""
        parsed = json.loads(data)
        
        fuel_stops = [
            FuelStop(
                opis_id=stop['opis_id'],
                name=stop['name'],
                city=stop['city'],
                state=stop['state'],
                latitude=stop['latitude'],
                longitude=stop['longitude'],
                price_per_gallon=stop['price_per_gallon'],
                distance_from_start=stop['distance_from_start'],
                distance_from_end=stop['distance_from_end'],
                gallons_needed=stop['gallons_needed'],
                cost=stop['cost']
            )
            for stop in parsed['fuel_stops']
        ]
        
        return RouteResult(
            start_location=parsed['start_location'],
            end_location=parsed['end_location'],
            total_distance_miles=parsed['total_distance_miles'],
            total_fuel_cost=Decimal(parsed['total_fuel_cost']),
            fuel_stops=fuel_stops,
            route_polyline=parsed['route_polyline'],
            timestamp=datetime.fromisoformat(parsed['timestamp']),
            cached=True
        )


# ============================================================================
# ROUTE SERVICE (GOOGLE ROUTES API WITH CACHING)
# ============================================================================

class RouteService:
    """
    Google Routes API integration with intelligent caching.
    
    Goal: Minimize API calls (1 per unique route is ideal)
    Strategy:
    - Cache routes for 24 hours
    - Return cached route if available
    - Single API call per route
    """
    
    API_ENDPOINT = "https://routes.googleapis.com/directions/api/directions/json"
    
    @classmethod
    def get_route(cls, start: str, end: str) -> Optional[Dict]:
        """
        Get route from Google Routes API.
        
        Returns:
        {
            'distance_meters': int,
            'distance_miles': float,
            'duration_seconds': int,
            'polyline': str,
            'waypoints': [(lat, lon), ...]
        }
        """
        if not settings.GOOGLE_MAPS_API_KEY:
            logger.error("GOOGLE_MAPS_API_KEY not configured")
            return None
        
        try:
            # ONE API call
            params = {
                'origin': f"{start}, USA",
                'destination': f"{end}, USA",
                'key': settings.GOOGLE_MAPS_API_KEY,
                'mode': 'driving'
            }
            
            response = requests.get(
                cls.API_ENDPOINT,
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') != 'OK':
                logger.error(f"Google API error: {data.get('error_message')}")
                return None
            
            route = data['routes'][0]
            leg = route['legs'][0]
            
            # Extract waypoints
            waypoints = []
            for step in leg['steps']:
                start_loc = step['start_location']
                waypoints.append((start_loc['lat'], start_loc['lng']))
            
            # Add end point
            end_loc = leg['end_location']
            waypoints.append((end_loc['lat'], end_loc['lng']))
            
            distance_meters = leg['distance']['value']
            distance_miles = distance_meters / 1609.34
            
            return {
                'distance_meters': distance_meters,
                'distance_miles': distance_miles,
                'duration_seconds': leg['duration']['value'],
                'polyline': route['overview_polyline']['points'],
                'waypoints': waypoints
            }
        
        except requests.exceptions.Timeout:
            logger.error("Google API request timed out")
            return None
        except Exception as e:
            logger.error(f"Error calling Google Routes API: {e}")
            return None


# ============================================================================
# FUEL STOP OPTIMIZER (MULTI-FACTOR OPTIMIZATION)
# ============================================================================

class FuelStopOptimizer:
    """
    Intelligent fuel stop selection.
    
    Optimization Factors:
    - Cost (40% weight): Lower price is better
    - Detour Distance (35% weight): Minimal detour from direct route
    - Route Position (25% weight): Strategic placement along route
    
    Algorithm:
    1. Divide route into segments (max 425-mile usable range)
    2. For each segment:
       a) Find nearby stops within 50-mile radius
       b) Score each stop by multi-factor formula
       c) Select lowest-score stop (best overall)
    """
    
    VEHICLE_FUEL_TANK = settings.FUEL_TANK_CAPACITY_GALLONS
    VEHICLE_MPG = settings.VEHICLE_FUEL_EFFICIENCY
    VEHICLE_RANGE = settings.MAX_VEHICLE_RANGE
    FUEL_RESERVE = 7.5  # gallons (15% safety buffer)
    USABLE_RANGE = (VEHICLE_FUEL_TANK - FUEL_RESERVE) * VEHICLE_MPG  # 425 miles
    
    @classmethod
    def find_optimal_stops(cls, 
                          route_waypoints: List[Tuple[float, float]],
                          total_distance_miles: float) -> List[FuelStop]:
        """
        Find optimal fuel stops along route.
        
        Process:
        1. Calculate number of stops needed
        2. Divide route into segments
        3. For each segment, find best stop
        4. Return sorted list of stops
        """
        if total_distance_miles <= cls.USABLE_RANGE:
            return []  # No stops needed, can reach in one tank
        
        # Calculate stops needed
        fuel_needed = total_distance_miles / cls.VEHICLE_MPG
        stops_needed = max(1, int((fuel_needed - cls.VEHICLE_FUEL_TANK) / (cls.VEHICLE_FUEL_TANK * 0.8)))
        
        fuel_stops = []
        current_fuel = cls.VEHICLE_FUEL_TANK
        distance_traveled = 0
        
        # Segment the route
        segment_length = total_distance_miles / (stops_needed + 1)
        
        for segment_idx in range(stops_needed):
            # Calculate point along route for this stop
            waypoint_idx = int((segment_idx + 1) * len(route_waypoints) / (stops_needed + 1))
            waypoint_idx = min(waypoint_idx, len(route_waypoints) - 1)
            
            segment_start = route_waypoints[waypoint_idx]
            segment_distance = (segment_idx + 1) * segment_length
            
            # Find nearby stops
            nearby = cls._find_nearby_stops(
                latitude=segment_start[0],
                longitude=segment_start[1],
                radius_miles=50
            )
            
            if not nearby:
                continue
            
            # Score stops
            best_stop = cls._select_best_stop(
                nearby,
                segment_start,
                route_waypoints[-1] if segment_idx < len(route_waypoints) - 1 else route_waypoints[-1],
                segment_distance
            )
            
            if best_stop:
                fuel_stops.append(best_stop)
                current_fuel = cls.VEHICLE_FUEL_TANK
        
        return fuel_stops
    
    @classmethod
    def _find_nearby_stops(cls, 
                          latitude: float, 
                          longitude: float,
                          radius_miles: float = 50) -> List[TruckStop]:
        """
        Find truck stops near given coordinates.
        
        Uses PostGIS distance calculation for accuracy.
        """
        lat_range = radius_miles / 69  # 1 degree ≈ 69 miles
        lon_range = radius_miles / 69
        
        return TruckStop.objects.filter(
            is_active=True,
            latitude__range=(latitude - lat_range, latitude + lat_range),
            longitude__range=(longitude - lon_range, longitude + lon_range)
        ).order_by('retail_price')[:30]  # Top 30 candidates
    
    @classmethod
    def _select_best_stop(cls,
                         candidates: List[TruckStop],
                         segment_point: Tuple[float, float],
                         route_end: Tuple[float, float],
                         distance_from_start: float) -> Optional[FuelStop]:
        """
        Select best stop from candidates using multi-factor scoring.
        
        Score = (price_factor * 0.40) + (detour_factor * 0.35) + (position_factor * 0.25)
        """
        best_score = float('inf')
        best_stop = None
        
        for stop in candidates:
            stop_coord = (stop.latitude, stop.longitude)
            
            # Factor 1: Price (40% weight)
            # Normalize: $2.50 = 100, $4.00 = 160
            price_factor = float(stop.retail_price) * 40
            
            # Factor 2: Detour distance (35% weight)
            direct_distance = cls._distance(segment_point, route_end)
            via_stop_distance = cls._distance(segment_point, stop_coord) + \
                               cls._distance(stop_coord, route_end)
            detour_miles = max(0, via_stop_distance - direct_distance)
            detour_factor = detour_miles * 3.5  # 10 miles = 35 points
            
            # Factor 3: Position (25% weight)
            # Prefer stops in "sweet spot" of route (not too early, not too late)
            segment_middle = cls.USABLE_RANGE / 2
            position_delta = abs(distance_from_start % cls.USABLE_RANGE - segment_middle)
            position_factor = (position_delta / segment_middle) * 25
            
            # Combined score
            score = price_factor + detour_factor + position_factor
            
            if score < best_score and detour_miles <= 50:  # Max 50-mile detour
                best_score = score
                best_stop = stop
        
        if best_stop:
            # Calculate fuel and cost
            distance_to_end = cls._distance(
                (best_stop.latitude, best_stop.longitude),
                route_end
            )
            fuel_needed = distance_to_end / cls.VEHICLE_MPG
            gallons_to_fill = cls.VEHICLE_FUEL_TANK - fuel_needed
            gallons_to_fill = max(0, gallons_to_fill)
            
            return FuelStop(
                opis_id=best_stop.opis_id,
                name=best_stop.name,
                city=best_stop.city,
                state=best_stop.state,
                latitude=best_stop.latitude,
                longitude=best_stop.longitude,
                price_per_gallon=float(best_stop.retail_price),
                distance_from_start=distance_from_start,
                distance_from_end=distance_to_end,
                gallons_needed=gallons_to_fill,
                cost=float(Decimal(str(best_stop.retail_price)) * Decimal(str(gallons_to_fill)))
            )
        
        return None
    
    @staticmethod
    def _distance(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
        """Calculate distance in miles using haversine formula."""
        lat1, lon1 = p1
        lat2, lon2 = p2
        
        # Simplified haversine
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = dlat * dlat + (dlon * 0.6) ** 2  # Lat/lon ratio correction
        distance_miles = (a ** 0.5) * 69
        
        return distance_miles


# ============================================================================
# GEO SERVICE (POSTGIS QUERIES)
# ============================================================================

class GeoService:
    """
    Geospatial queries using PostGIS.
    
    Provides:
    - Distance-based stop search
    - State-level aggregations
    - Route coverage analysis
    """
    
    @classmethod
    def get_stops_by_state(cls, state: str) -> List[TruckStop]:
        """Get all active stops in a state, sorted by price."""
        return TruckStop.objects.filter(
            state=state.upper(),
            is_active=True
        ).order_by('retail_price')
    
    @classmethod
    def get_cheapest_in_state(cls, state: str) -> Optional[TruckStop]:
        """Get cheapest stop in a state."""
        return TruckStop.objects.filter(
            state=state.upper(),
            is_active=True
        ).order_by('retail_price').first()
    
    @classmethod
    def get_expensive_in_state(cls, state: str) -> Optional[TruckStop]:
        """Get most expensive stop in a state."""
        return TruckStop.objects.filter(
            state=state.upper(),
            is_active=True
        ).order_by('-retail_price').first()
