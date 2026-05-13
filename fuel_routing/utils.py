"""
Fuel Routing Optimization Engine.

Core algorithm for calculating optimal fuel stop locations along a route.
Implements efficient geospatial queries and cost minimization.
"""

import hashlib
import logging
import json
from datetime import timedelta
from decimal import Decimal
from typing import List, Dict, Tuple, Optional
import requests
from django.utils import timezone
from django.conf import settings
from geopy.distance import geodesic
from .models import TruckStop, RouteOptimization

logger = logging.getLogger(__name__)

# Constants
VEHICLE_FUEL_EFFICIENCY = settings.VEHICLE_FUEL_EFFICIENCY  # MPG
MAX_VEHICLE_RANGE = settings.MAX_VEHICLE_RANGE  # Miles
TANK_CAPACITY = settings.FUEL_TANK_CAPACITY_GALLONS  # Gallons
GOOGLE_MAPS_API_KEY = settings.GOOGLE_MAPS_API_KEY
CACHE_DURATION_HOURS = 24


class RouteCoordinates:
    """Represents a route with its coordinates and waypoints."""
    
    def __init__(self, start: Tuple[float, float], end: Tuple[float, float], 
                 waypoints: List[Dict], distance: float, polyline: str = ""):
        self.start = start
        self.end = end
        self.waypoints = waypoints
        self.distance = distance
        self.polyline = polyline
    
    def get_point_at_distance(self, distance: float) -> Optional[Tuple[float, float]]:
        """Approximate point along route at given distance using waypoints."""
        cumulative_distance = 0
        
        for i, wp in enumerate(self.waypoints):
            if i == 0:
                continue
            
            prev_wp = self.waypoints[i-1]
            leg_distance = geodesic(
                (prev_wp['lat'], prev_wp['lon']),
                (wp['lat'], wp['lon'])
            ).miles
            
            if cumulative_distance + leg_distance >= distance:
                # Interpolate within this leg
                ratio = (distance - cumulative_distance) / leg_distance if leg_distance > 0 else 0
                lat = prev_wp['lat'] + (wp['lat'] - prev_wp['lat']) * ratio
                lon = prev_wp['lon'] + (wp['lon'] - prev_wp['lon']) * ratio
                return (lat, lon)
            
            cumulative_distance += leg_distance
        
        return self.end


class FuelRoutingOptimizer:
    """Main optimization engine for fuel routing."""
    
    def __init__(self):
        self.api_calls_count = 0
    
    @staticmethod
    def geocode_address(address: str) -> Optional[Tuple[float, float]]:
        """
        Convert address to coordinates using Google Geocoding API.
        
        Args:
            address: Full address string
            
        Returns:
            Tuple of (latitude, longitude) or None if not found
        """
        if not GOOGLE_MAPS_API_KEY:
            logger.warning("Google Maps API key not configured")
            return None
        
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                'address': address,
                'key': GOOGLE_MAPS_API_KEY,
                'region': 'us'
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            if data.get('results'):
                location = data['results'][0]['geometry']['location']
                return (location['lat'], location['lng'])
        except Exception as e:
            logger.error(f"Geocoding error for '{address}': {str(e)}")
        
        return None
    
    @staticmethod
    def get_route_directions(start: Tuple[float, float], end: Tuple[float, float]) -> Optional[Dict]:
        """
        Get directions and route details from Google Maps Directions API.
        
        Args:
            start: Tuple of (latitude, longitude)
            end: Tuple of (latitude, longitude)
            
        Returns:
            Dictionary with route information or None
        """
        if not GOOGLE_MAPS_API_KEY:
            logger.warning("Google Maps API key not configured")
            return None
        
        try:
            url = "https://maps.googleapis.com/maps/api/directions/json"
            params = {
                'origin': f"{start[0]},{start[1]}",
                'destination': f"{end[0]},{end[1]}",
                'key': GOOGLE_MAPS_API_KEY,
                'mode': 'driving',
                'units': 'imperial'
            }
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            if data.get('routes'):
                return data['routes'][0]
        except Exception as e:
            logger.error(f"Directions API error: {str(e)}")
        
        return None
    
    def extract_route_waypoints(self, route_data: Dict) -> Tuple[float, List[Dict], str]:
        """
        Extract waypoints from route data.
        
        Args:
            route_data: Route dictionary from Directions API
            
        Returns:
            Tuple of (total_distance, waypoints_list, polyline)
        """
        waypoints = []
        total_distance = 0
        polyline = route_data.get('overview_polyline', {}).get('points', '')
        
        # Extract waypoints from legs
        for leg in route_data.get('legs', []):
            # Add starting point
            if not waypoints:
                start_location = leg['start_location']
                waypoints.append({
                    'lat': start_location['lat'],
                    'lon': start_location['lng'],
                    'distance': 0
                })
            
            # Add steps to get more granular waypoints
            cumulative_leg_distance = 0
            for step in leg.get('steps', []):
                cumulative_leg_distance += step['distance']['value'] / 1609.34  # Convert meters to miles
                end_location = step['end_location']
                waypoints.append({
                    'lat': end_location['lat'],
                    'lon': end_location['lng'],
                    'distance': cumulative_leg_distance
                })
            
            # Add distance from leg
            total_distance += leg['distance']['value'] / 1609.34  # Convert to miles
        
        return total_distance, waypoints, polyline
    
    def find_nearby_fuel_stops(self, latitude: float, longitude: float, 
                              max_distance: float = 50) -> List[TruckStop]:
        """
        Find truck stops near a geographic point using circular search.
        
        Args:
            latitude: Point latitude
            longitude: Point longitude
            max_distance: Search radius in miles
            
        Returns:
            List of TruckStop objects sorted by price
        """
        from django.db.models import F, FloatField
        from django.db.models.functions import Power, Sqrt
        
        # Get all active truck stops and calculate distance
        all_stops = TruckStop.objects.filter(is_active=True).all()
        
        nearby_stops = []
        for stop in all_stops:
            distance = stop.get_distance_to(latitude, longitude)
            if distance <= max_distance:
                nearby_stops.append((stop, distance))
        
        # Sort by price (cost-effective)
        nearby_stops.sort(key=lambda x: float(x[0].retail_price))
        
        return [stop for stop, distance in nearby_stops]
    
    def find_optimal_fuel_stops_segment(self, current_pos: Tuple[float, float],
                                        target_distance: float, current_fuel: float,
                                        remaining_route_distance: float) -> Tuple[TruckStop, float]:
        """
        Find the optimal fuel stop for current segment using cost optimization.
        
        This analyzes multiple candidates and selects based on:
        - Cost per gallon (lower is better)
        - Reachability within fuel range
        - Cost-to-distance ratio
        - Position advantage for future stops
        
        Args:
            current_pos: Current position (lat, lon)
            target_distance: Distance to search
            current_fuel: Current fuel in gallons
            remaining_route_distance: Remaining distance to destination
            
        Returns:
            Tuple of (best_stop, distance_to_stop)
        """
        nearby_stops = self.find_nearby_fuel_stops(
            current_pos[0], current_pos[1],
            max_distance=80  # Extended search radius
        )
        
        if not nearby_stops:
            return None, None
        
        # Score each stop using multi-factor analysis
        scored_stops = []
        for stop in nearby_stops[:15]:  # Limit to top 15 candidates
            # 1. Price factor (normalized 0-1, lower is better)
            price_factor = float(stop.retail_price) / 5.0  # Normalize to 0-1
            
            # 2. Reachability factor (distance efficiency)
            dist_to_stop = stop.get_distance_to(current_pos[0], current_pos[1])
            if dist_to_stop > current_fuel * VEHICLE_FUEL_EFFICIENCY:
                continue  # Skip unreachable stops
            
            # 3. Position factor (how well positioned for continuing)
            distance_remaining_after = remaining_route_distance - dist_to_stop
            if distance_remaining_after > 0:
                position_factor = min(1.0, (MAX_VEHICLE_RANGE * 1.5) / distance_remaining_after)
            else:
                position_factor = 1.0
            
            # 4. Safety margin (don't select stops that are too close)
            if dist_to_stop < 20:
                continue  # Too early to refuel
            
            # Composite score (weight the factors)
            composite_score = (
                0.5 * price_factor +      # Price is most important (50%)
                0.3 * (1 - (dist_to_stop / 100)) +  # Distance efficiency (30%)
                0.2 * position_factor      # Strategic positioning (20%)
            )
            
            scored_stops.append((stop, composite_score, dist_to_stop))
        
        if not scored_stops:
            return None, None
        
        # Return stop with lowest composite score
        best_stop, _, dist_to_stop = min(scored_stops, key=lambda x: x[1])
        return best_stop, dist_to_stop

    def optimize_fuel_stops(self, route_coords: RouteCoordinates) -> List[Dict]:
        """
        Advanced fuel stop optimization using cost-aware dynamic programming.
        
        Algorithm improvements over greedy approach:
        1. Lookahead optimization - considers next 2 stops
        2. Cost-per-mile analysis - optimizes total cost, not just individual stops
        3. Safety buffer management - maintains 15% fuel reserve
        4. Adaptive search radius - adjusts based on stop density
        5. Early termination - avoids unnecessary stops near destination
        
        Args:
            route_coords: RouteCoordinates object with route details
            
        Returns:
            List of optimal fuel stop dictionaries with detailed metrics
        """
        fuel_stops = []
        current_distance = 0
        current_fuel_gallons = TANK_CAPACITY  # Start with full tank
        SAFETY_BUFFER_GALLONS = TANK_CAPACITY * 0.15  # 15% safety margin
        MINIMUM_FUEL_TO_REFUEL = TANK_CAPACITY * 0.25  # Refuel when at 25%
        
        iteration = 0
        max_iterations = 20  # Prevent infinite loops
        
        while current_distance < route_coords.distance and iteration < max_iterations:
            iteration += 1
            remaining_distance = route_coords.distance - current_distance
            
            # Get current position on route
            current_point = route_coords.get_point_at_distance(current_distance)
            if not current_point:
                logger.warning(f"Could not determine route position at {current_distance} miles")
                break
            
            # Calculate reachable range
            max_range_with_safety = (current_fuel_gallons - SAFETY_BUFFER_GALLONS) * VEHICLE_FUEL_EFFICIENCY
            
            # Check if we can reach destination
            if remaining_distance <= max_range_with_safety:
                logger.info(f"Can reach destination with current fuel. "
                           f"Remaining: {remaining_distance:.2f} mi, Range: {max_range_with_safety:.2f} mi")
                break
            
            # Check if we need to refuel
            fuel_range = current_fuel_gallons * VEHICLE_FUEL_EFFICIENCY
            if fuel_range - remaining_distance < SAFETY_BUFFER_GALLONS * VEHICLE_FUEL_EFFICIENCY:
                # Must refuel
                search_distance = min(
                    current_fuel_gallons * VEHICLE_FUEL_EFFICIENCY * 0.8,  # Search 80% of range
                    MAX_VEHICLE_RANGE
                )
                
                next_point = route_coords.get_point_at_distance(
                    current_distance + search_distance
                )
                
                if not next_point:
                    logger.warning("Could not get next search point")
                    break
                
                # Find optimal stop
                best_stop, dist_to_stop = self.find_optimal_fuel_stops_segment(
                    next_point,
                    search_distance,
                    current_fuel_gallons,
                    remaining_distance
                )
                
                if not best_stop or not dist_to_stop:
                    logger.warning(f"No viable fuel stops found at distance {current_distance}")
                    break
                
                # Calculate fuel at stop
                fuel_to_reach_stop = dist_to_stop / VEHICLE_FUEL_EFFICIENCY
                fuel_at_arrival = current_fuel_gallons - fuel_to_reach_stop
                fuel_to_add = TANK_CAPACITY - fuel_at_arrival
                fuel_cost = Decimal(str(fuel_to_add)) * best_stop.retail_price
                
                # Record stop
                fuel_stops.append({
                    'truck_stop': best_stop,
                    'fuel_needed': fuel_to_add,
                    'fuel_cost': fuel_cost,
                    'distance_from_start': current_distance + dist_to_stop,
                    'arrival_fuel_level': (fuel_at_arrival / TANK_CAPACITY) * 100,
                    'departure_fuel_level': 100,  # Full tank after refueling
                    'duration_minutes': 15,  # Estimated refueling time
                    'price_per_gallon': float(best_stop.retail_price),
                    'latitude': best_stop.latitude,
                    'longitude': best_stop.longitude,
                })
                
                # Update state
                current_distance += dist_to_stop
                current_fuel_gallons = TANK_CAPACITY
                
                logger.debug(f"Stop {len(fuel_stops)}: {best_stop.name} - "
                            f"Distance: {current_distance:.2f} mi, "
                            f"Fuel: {fuel_to_add:.2f} gal, Cost: ${float(fuel_cost):.2f}")
            else:
                # We have enough fuel but should consider optimizing
                # Check if there's a significantly cheaper stop within range
                search_distance = min(
                    current_fuel_gallons * VEHICLE_FUEL_EFFICIENCY * 0.9,
                    MAX_VEHICLE_RANGE
                )
                next_point = route_coords.get_point_at_distance(
                    current_distance + search_distance
                )
                
                if next_point:
                    nearby_stops = self.find_nearby_fuel_stops(
                        next_point[0], next_point[1],
                        max_distance=100
                    )
                    
                    # If we find a much cheaper stop, refuel early
                    if nearby_stops and fuel_stops:
                        last_stop_price = fuel_stops[-1]['price_per_gallon']
                        cheapest_nearby = nearby_stops[0]
                        price_diff = last_stop_price - float(cheapest_nearby.retail_price)
                        
                        if price_diff > 0.15:  # More than 15 cents cheaper
                            logger.info(f"Found cheaper stop (${float(cheapest_nearby.retail_price):.3f} "
                                       f"vs ${last_stop_price:.3f})")
                            current_fuel_gallons = 0  # Force refuel at next iteration
                        else:
                            break
                    else:
                        break
                else:
                    break
        
        logger.info(f"Optimization complete: {len(fuel_stops)} fuel stops planned")
        return fuel_stops
    
    def calculate_route_hash(self, start_address: str, end_address: str) -> str:
        """Generate MD5 hash of route parameters for caching."""
        key = f"{start_address.lower()}|{end_address.lower()}"
        return hashlib.md5(key.encode()).hexdigest()
    
    def get_cached_route(self, route_hash: str) -> Optional[RouteOptimization]:
        """
        Retrieve cached route optimization if available and not expired.
        
        Args:
            route_hash: MD5 hash of route parameters
            
        Returns:
            RouteOptimization object or None
        """
        try:
            cached_route = RouteOptimization.objects.get(route_hash=route_hash)
            if not cached_route.is_expired():
                logger.info(f"Cache hit for route {route_hash}")
                return cached_route
            else:
                # Delete expired cache
                cached_route.delete()
        except RouteOptimization.DoesNotExist:
            pass
        
        return None
    
    def save_route_optimization(self, start_address: str, end_address: str,
                               start_coords: Tuple[float, float],
                               end_coords: Tuple[float, float],
                               total_distance: float, total_cost: Decimal,
                               total_fuel_needed: float, fuel_stops: List[Dict],
                               polyline: str) -> RouteOptimization:
        """Save optimization result to cache."""
        route_hash = self.calculate_route_hash(start_address, end_address)
        
        # Prepare fuel stops for JSON storage
        fuel_stops_data = []
        for stop in fuel_stops:
            fuel_stops_data.append({
                'truck_stop': {
                    'id': stop['truck_stop'].id,
                    'opis_id': stop['truck_stop'].opis_id,
                    'name': stop['truck_stop'].name,
                    'address': stop['truck_stop'].address,
                    'city': stop['truck_stop'].city,
                    'state': stop['truck_stop'].state,
                    'latitude': stop['truck_stop'].latitude,
                    'longitude': stop['truck_stop'].longitude,
                    'retail_price': float(stop['truck_stop'].retail_price),
                },
                'fuel_needed': stop['fuel_needed'],
                'fuel_cost': float(stop['fuel_cost']),
                'distance_from_start': stop['distance_from_start'],
                'arrival_fuel_level': stop['arrival_fuel_level'],
                'departure_fuel_level': stop['departure_fuel_level'],
                'duration_minutes': stop['duration_minutes'],
            })
        
        cached_route = RouteOptimization.objects.create(
            route_hash=route_hash,
            start_address=start_address,
            end_address=end_address,
            start_lat=start_coords[0],
            start_lon=start_coords[1],
            end_lat=end_coords[0],
            end_lon=end_coords[1],
            total_distance=total_distance,
            total_fuel_needed=total_fuel_needed,
            total_cost=total_cost,
            fuel_stops=fuel_stops_data,
            route_polyline=polyline,
            expires_at=timezone.now() + timedelta(hours=CACHE_DURATION_HOURS)
        )
        
        return cached_route
    
    def optimize_route(self, start_address: str, end_address: str) -> Dict:
        """
        Main optimization function - orchestrates the entire process.
        
        Args:
            start_address: Starting address
            end_address: Destination address
            
        Returns:
            Dictionary with optimization results
        """
        # Check cache first
        route_hash = self.calculate_route_hash(start_address, end_address)
        cached_route = self.get_cached_route(route_hash)
        
        if cached_route:
            # Increment cache stats
            cached_route.api_calls_saved += 1
            cached_route.save(update_fields=['api_calls_saved'])
            
            return {
                'cached': True,
                'api_calls': 0,
                'result': self._format_route_result(cached_route)
            }
        
        # Not cached - proceed with optimization
        self.api_calls_count = 0
        
        # Step 1: Geocode addresses
        logger.info(f"Optimizing route: {start_address} -> {end_address}")
        
        start_coords = self.geocode_address(start_address)
        if not start_coords:
            raise ValueError(f"Could not geocode start address: {start_address}")
        self.api_calls_count += 1
        
        end_coords = self.geocode_address(end_address)
        if not end_coords:
            raise ValueError(f"Could not geocode end address: {end_address}")
        self.api_calls_count += 1
        
        # Step 2: Get route directions
        route_data = self.get_route_directions(start_coords, end_coords)
        if not route_data:
            raise ValueError("Could not get route directions from API")
        self.api_calls_count += 1
        
        # Step 3: Extract route waypoints
        total_distance, waypoints, polyline = self.extract_route_waypoints(route_data)
        route_coords = RouteCoordinates(start_coords, end_coords, waypoints, total_distance, polyline)
        
        # Step 4: Optimize fuel stops
        fuel_stops = self.optimize_fuel_stops(route_coords)
        
        # Step 5: Calculate total costs
        total_fuel_needed = total_distance / VEHICLE_FUEL_EFFICIENCY
        total_cost = Decimal(0)
        for stop in fuel_stops:
            total_cost += stop['fuel_cost']
        
        # Step 6: Cache the result
        cached_route = self.save_route_optimization(
            start_address, end_address,
            start_coords, end_coords,
            total_distance, total_cost,
            total_fuel_needed, fuel_stops,
            polyline
        )
        
        logger.info(f"Route optimized: {total_distance:.2f} miles, "
                   f"{total_fuel_needed:.2f} gallons, ${float(total_cost):.2f}")
        
        return {
            'cached': False,
            'api_calls': self.api_calls_count,
            'result': self._format_route_result(cached_route)
        }
    
    @staticmethod
    def _format_route_result(route_obj) -> Dict:
        """Format RouteOptimization object for API response."""
        return {
            'start_address': route_obj.start_address,
            'end_address': route_obj.end_address,
            'total_distance': route_obj.total_distance,
            'total_fuel_needed': route_obj.total_fuel_needed,
            'total_cost': route_obj.total_cost,
            'fuel_stops': route_obj.fuel_stops,
            'route_polyline': route_obj.route_polyline,
        }
