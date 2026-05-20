"""Fuel station query service with polyline corridor filtering."""
import logging
from typing import Any, Dict, List, Optional

from .constants import CORRIDOR_BUFFER_MILES
from .geocoding import _fast_distance_miles
from .route_geometry import decode_route_to_coordinates
from .routing import RouteAlternative
from .cache_utils import CorridorStationCache

logger = logging.getLogger(__name__)


class FuelStationQueryService:
    """Query fuel stations using distance-based filtering with polyline corridor validation."""

    @staticmethod
    def filter_stations_by_route(
        route: RouteAlternative,
        pre_queried_stations: List[Dict[str, Any]],
        buffer_miles: float = CORRIDOR_BUFFER_MILES
    ) -> List[Dict[str, Any]]:
        """Filter pre-queried stations by route's polyline corridor.

        Uses CorridorStationCache (Redis) to skip repeated corridor filtering
        for the same route. Cached by OPIS IDs only (excludes price data since
        prices change regularly but station locations do not).
        """
        if not pre_queried_stations:
            return []

        # Check corridor station cache
        cached_ids = CorridorStationCache.get(route.route_id, buffer_miles, route.polyline_encoded or '')
        if cached_ids is not None:
            id_set = set(cached_ids)
            filtered = [s for s in pre_queried_stations if s['opis_id'] in id_set]
            logger.info(
                f"Corridor cache HIT: {len(filtered)} stations for {route.route_id} "
                f"from {len(pre_queried_stations)} candidates"
            )
            return filtered

        try:
            polyline_coords = None
            sampled_polyline_coords = None
            if route.polyline_encoded:
                try:
                    polyline_coords = decode_route_to_coordinates(route.polyline_encoded)
                    sample_rate = max(1, len(polyline_coords) // 50)
                    sampled_polyline_coords = polyline_coords[::sample_rate]
                except Exception as e:
                    logger.warning(f"Failed to decode polyline for filtering: {e}")

            sw = route.bounds.get('sw', {})
            ne = route.bounds.get('ne', {})
            start_lat = float(sw.get('lat', 0))
            start_lon = float(sw.get('lng', 0))
            end_lat = float(ne.get('lat', 0))
            end_lon = float(ne.get('lng', 0))

            # Pre-compute tight polyline bbox for quick rejection (avoids haversine for distant stations)
            poly_points = list(sampled_polyline_coords or [])
            if poly_points:
                poly_lats = [p[0] for p in poly_points]
                poly_lons = [p[1] for p in poly_points]
                poly_lat_min, poly_lat_max = min(poly_lats), max(poly_lats)
                poly_lon_min, poly_lon_max = min(poly_lons), max(poly_lons)
                deg_buffer = buffer_miles / 69.0
            else:
                poly_lat_min = min(start_lat, end_lat) - 2.0
                poly_lat_max = max(start_lat, end_lat) + 2.0
                poly_lon_min = min(start_lon, end_lon) - 2.0
                poly_lon_max = max(start_lon, end_lon) + 2.0
                deg_buffer = 0.0

            filtered_stations = []

            for station in pre_queried_stations:
                sta_lat = float(station['latitude'])
                sta_lon = float(station['longitude'])

                # Fast bbox check (no trig) before expensive haversine loop
                in_corridor_bbox = (
                    poly_lat_min - deg_buffer <= sta_lat <= poly_lat_max + deg_buffer and
                    poly_lon_min - deg_buffer <= sta_lon <= poly_lon_max + deg_buffer
                )

                if not in_corridor_bbox:
                    continue

                if poly_points:
                    for plat, plon in poly_points:
                        if _fast_distance_miles(sta_lat, sta_lon, plat, plon) <= buffer_miles:
                            filtered_stations.append(station)
                            break
                else:
                    filtered_stations.append(station)

            # Cache filtered OPIS IDs for this route corridor
            opis_ids = [s['opis_id'] for s in filtered_stations]
            CorridorStationCache.set(route.route_id, buffer_miles, opis_ids, route.polyline_encoded or '')

            logger.info(
                f"Filtered {len(filtered_stations)} stations for "
                f"{route.route_id} from {len(pre_queried_stations)} candidates"
            )
            return filtered_stations

        except Exception as e:
            logger.error(f"Error filtering stations: {e}", exc_info=True)
            return []
