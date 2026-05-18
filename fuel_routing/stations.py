"""Fuel station query service with polyline corridor filtering."""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings

from .constants import CORRIDOR_BUFFER_MILES, MAX_DETOUR_MILES
from .geocoding import Location, _fast_distance_miles
from .models import FuelPrice, FuelStation, PriceVersion
from .route_geometry import decode_route_to_coordinates
from .routing import RouteAlternative
from .cache_utils import CorridorStationCache

logger = logging.getLogger(__name__)


class FuelStationQueryService:
    """Query fuel stations using distance-based filtering with polyline corridor validation."""

    @staticmethod
    def get_stations_in_corridor(
        route: RouteAlternative,
        buffer_miles: float = CORRIDOR_BUFFER_MILES,
        price_lookup: Optional[Dict[int, Decimal]] = None
    ) -> List[Dict[str, Any]]:
        """Query fuel stations within route corridor using distance-based filtering."""
        logger.info(f"Querying fuel stations in corridor for {route.route_id}")

        price_version = PriceVersion.objects.filter(is_active=True).first()
        if not price_version:
            logger.warning("No active price version found")
            return []

        if price_lookup is None:
            price_lookup = dict(
                FuelPrice.objects.filter(version=price_version).values_list('opis_id', 'price_per_gallon')
            )

        try:
            sw = route.bounds.get('sw', {})
            ne = route.bounds.get('ne', {})
            start_lat = float(sw.get('lat', 0))
            start_lon = float(sw.get('lng', 0))
            end_lat = float(ne.get('lat', 0))
            end_lon = float(ne.get('lng', 0))
        except Exception as e:
            logger.warning(f"Failed to extract route bounds: {e}")
            return []

        stations_data = []
        try:
            start_loc = Location(start_lat, start_lon)
            end_loc = Location(end_lat, end_lon)

            polyline_coords = None
            sampled_polyline_coords = None
            if route.polyline_encoded:
                try:
                    polyline_coords = decode_route_to_coordinates(route.polyline_encoded)
                    sample_rate = max(1, len(polyline_coords) // 200)
                    sampled_polyline_coords = polyline_coords[::sample_rate]
                except Exception as e:
                    logger.warning(f"Failed to decode polyline: {e}")

            lat_min = min(start_lat, end_lat) - 3.0
            lat_max = max(start_lat, end_lat) + 3.0
            lon_min = min(start_lon, end_lon) - 3.0
            lon_max = max(start_lon, end_lon) + 3.0

            candidate_stations = FuelStation.objects.filter(
                is_active=True,
                latitude__gte=lat_min,
                latitude__lte=lat_max,
                longitude__gte=lon_min,
                longitude__lte=lon_max
            ).values(
                'opis_id', 'name', 'address', 'city', 'state', 'latitude', 'longitude'
            )

            for station in candidate_stations:
                station_loc = Location(float(station['latitude']), float(station['longitude']))

                dist_to_start = station_loc.distance_to(start_loc)
                dist_to_end = station_loc.distance_to(end_loc)
                route_distance = start_loc.distance_to(end_loc)

                is_near_start = dist_to_start <= buffer_miles + 100
                is_near_end = dist_to_end <= buffer_miles + 100
                is_in_route_box = (lat_min <= station_loc.latitude <= lat_max and
                                  lon_min <= station_loc.longitude <= lon_max)

                is_near_polyline = False
                if sampled_polyline_coords and (is_near_start or is_near_end or is_in_route_box):
                    for lat, lon in sampled_polyline_coords:
                        checkpoint_loc = Location(lat, lon)
                        dist_to_checkpoint = station_loc.distance_to(checkpoint_loc)
                        if dist_to_checkpoint <= buffer_miles:
                            is_near_polyline = True
                            break
                elif is_near_start or is_near_end or is_in_route_box:
                    is_near_polyline = True

                if is_near_polyline:
                    price_per_gallon = price_lookup.get(station['opis_id'])
                    if price_per_gallon and price_per_gallon > 0:
                        stations_data.append({
                            'opis_id': station['opis_id'],
                            'name': station['name'],
                            'address': station.get('address', ''),
                            'city': station['city'],
                            'state': station['state'],
                            'latitude': station['latitude'],
                            'longitude': station['longitude'],
                            'price_per_gallon': price_per_gallon,
                        })

            logger.info(f"Found {len(stations_data)} stations in corridor")
            return stations_data

        except Exception as e:
            logger.error(f"Error querying fuel stations: {e}", exc_info=True)
            return []

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
        cached_ids = CorridorStationCache.get(route.route_id, buffer_miles)
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

            filtered_stations = []
            poly_points = list(sampled_polyline_coords or [])

            for station in pre_queried_stations:
                sta_lat = float(station['latitude'])
                sta_lon = float(station['longitude'])

                dist_to_start = _fast_distance_miles(sta_lat, sta_lon, start_lat, start_lon)
                dist_to_end = _fast_distance_miles(sta_lat, sta_lon, end_lat, end_lon)

                is_near_start = dist_to_start <= buffer_miles + 100
                is_near_end = dist_to_end <= buffer_miles + 100

                lat_min = min(start_lat, end_lat) - 2.0
                lat_max = max(start_lat, end_lat) + 2.0
                lon_min = min(start_lon, end_lon) - 2.0
                lon_max = max(start_lon, end_lon) + 2.0

                is_in_route_box = (lat_min <= sta_lat <= lat_max and
                                  lon_min <= sta_lon <= lon_max)

                is_near_polyline = False
                if poly_points and (is_near_start or is_near_end or is_in_route_box):
                    for plat, plon in poly_points:
                        if _fast_distance_miles(sta_lat, sta_lon, plat, plon) <= buffer_miles:
                            is_near_polyline = True
                            break
                elif is_near_start or is_near_end or is_in_route_box:
                    is_near_polyline = True

                if is_near_polyline:
                    filtered_stations.append(station)

            # Cache filtered OPIS IDs for this route corridor
            opis_ids = [s['opis_id'] for s in filtered_stations]
            CorridorStationCache.set(route.route_id, buffer_miles, opis_ids)

            logger.info(
                f"Filtered {len(filtered_stations)} stations for "
                f"{route.route_id} from {len(pre_queried_stations)} candidates"
            )
            return filtered_stations

        except Exception as e:
            logger.error(f"Error filtering stations: {e}", exc_info=True)
            return []
