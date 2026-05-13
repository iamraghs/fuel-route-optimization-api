"""PostGIS-based route corridor validation."""
import logging
from django.contrib.gis.geos import Point, LineString
from django.contrib.gis.db.models.functions import Distance
from django.db.models import F
from geopy.distance import geodesic

from .models import FuelStation

logger = logging.getLogger(__name__)


class CorridorValidator:
    """Validate fuel stops actually lie on route using PostGIS."""
    
    @staticmethod
    def validate_station_on_route(
        station_latitude: float,
        station_longitude: float,
        route_polyline_coords: list,
        max_distance_miles: float = 5.0
    ) -> bool:
        """
        Check if station is within max_distance of any point on route polyline.
        
        Args:
            station_latitude: Station lat
            station_longitude: Station lon
            route_polyline_coords: List of (lat, lon) tuples from decoded polyline
            max_distance_miles: Maximum distance in miles from polyline
            
        Returns:
            True if station is close enough to polyline, False otherwise
        """
        station_point = (station_latitude, station_longitude)
        
        # Find minimum distance to any segment of the polyline
        min_distance = float('inf')
        
        for i in range(len(route_polyline_coords) - 1):
            point1 = route_polyline_coords[i]
            point2 = route_polyline_coords[i + 1]
            
            # Distance to this segment (simplified: min of distances to endpoints)
            dist1 = geodesic(station_point, point1).miles
            dist2 = geodesic(station_point, point2).miles
            
            segment_min = min(dist1, dist2)
            min_distance = min(min_distance, segment_min)
            
            # Early exit if close enough
            if min_distance < max_distance_miles:
                return True
        
        return min_distance < max_distance_miles
    
    @staticmethod
    def filter_stations_to_corridor(
        stations: list,
        route_polyline_coords: list,
        max_distance_miles: float = 10.0
    ) -> list:
        """
        Filter stations to only those within corridor of route.
        
        Args:
            stations: List of station dicts with latitude/longitude
            route_polyline_coords: List of (lat, lon) tuples
            max_distance_miles: Corridor width in miles
            
        Returns:
            Filtered list of valid stations
        """
        valid_stations = []
        rejected = 0
        
        for station in stations:
            if CorridorValidator.validate_station_on_route(
                float(station['latitude']),
                float(station['longitude']),
                route_polyline_coords,
                max_distance_miles
            ):
                valid_stations.append(station)
            else:
                rejected += 1
        
        if rejected > 0:
            logger.info(f"Corridor validation: {len(stations)} → {len(valid_stations)} (rejected {rejected} off-route)")
        
        return valid_stations
