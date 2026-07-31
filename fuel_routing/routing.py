"""Routing service using Google Directions API with caching and request coalescing."""
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
from django.contrib.gis.geos import LineString
from django.db.models import F
from django.utils import timezone

import polyline

from .cache_utils import EnhancedCacheKeyGenerator, RequestLockManager
from .constants import GOOGLE_API_KEY, CACHE_TTL_ROUTE, GOOGLE_ROUTES_TIMEOUT
from .geocoding import Location
from .models import RouteCache

logger = logging.getLogger(__name__)


# HTTP connection pool for Google API calls (keep-alive, reused TCP connections)
_http_session = requests.Session()
_http_session.headers.update({'User-Agent': 'SpotterAI-FuelRouter/1.0'})
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=4,
    pool_maxsize=8,
    max_retries=0,
    pool_block=False
)
_http_session.mount('https://', _adapter)
_http_session.mount('http://', _adapter)


@dataclass
class RouteAlternative:
    """Single route option with geometry and metadata."""
    route_id: str
    distance_miles: float
    duration_seconds: int
    polyline_encoded: str
    bounds: Dict[str, Any]

    def duration_hours(self) -> float:
        return self.duration_seconds / 3600


class RoutingService:
    """Get route alternatives from Google Routes API with caching and request coalescing."""

    @classmethod
    def get_routes(
        cls,
        start: Location,
        end: Location,
        max_alternatives: int = 2,
        request_id: str = ''
    ) -> List[RouteAlternative]:
        """Get route alternatives with multi-route database caching."""
        cache_key = EnhancedCacheKeyGenerator.route_key(
            start.latitude, start.longitude, end.latitude, end.longitude
        )
        base_key = cache_key  # Key for all alternatives between this start/end
        lock_key = EnhancedCacheKeyGenerator.lock_key(cache_key)

        # Step 1: Check RouteCache for any cached alternatives
        cached_routes = cls._get_cached_alternatives(base_key)
        if cached_routes is not None:
            return cached_routes

        # Step 2: Use request coalescing to prevent duplicate Google API calls
        def compute_routes():
            logger.info(f"[{request_id}] Calling Google Directions API: {start.as_tuple()} -> {end.as_tuple()}")
            try:
                directions_url = "https://maps.googleapis.com/maps/api/directions/json"

                params = {
                    'origin': f"{start.latitude},{start.longitude}",
                    'destination': f"{end.latitude},{end.longitude}",
                    'key': GOOGLE_API_KEY,
                    'alternatives': 'true',
                    'mode': 'driving',
                    'units': 'imperial'
                }

                logger.info(f"Request params: origin={params['origin']}, destination={params['destination']}")

                response = _http_session.get(
                    directions_url,
                    params=params,
                    timeout=GOOGLE_ROUTES_TIMEOUT
                )

                logger.info(f"Google API Response Status: {response.status_code}")

                if response.status_code != 200:
                    logger.error(f"Google API returned {response.status_code}: {response.text}")
                    response.raise_for_status()

                data = response.json()

                if data.get('status') != 'OK':
                    error_msg = f"Google API status: {data.get('status')} - {data.get('error_message', 'Unknown error')}"
                    logger.error(f"Error: {error_msg}")
                    # ZERO_RESULTS = no drivable route exists (e.g. island,
                    # national park, or same-location edge). Return a clean
                    # client error (400), not a 500.
                    if data.get('status') == 'ZERO_RESULTS':
                        raise ValueError(
                            "No drivable route exists between the given locations."
                        )
                    raise Exception(error_msg)

                routes_list = []
                routes_data = data.get('routes', [])
                logger.info(f"[{request_id}] Got {len(routes_data)} routes from Google API")

                for i, route in enumerate(routes_data[:max_alternatives]):
                    try:
                        leg = route['legs'][0]
                        polyline_data = route['overview_polyline']['points']

                        distance_m = leg['distance']['value']
                        duration_s = leg['duration']['value']

                        route_alt = RouteAlternative(
                            route_id=f"route_{chr(97 + i)}",
                            distance_miles=distance_m * 0.000621371,
                            duration_seconds=duration_s,
                            polyline_encoded=polyline_data,
                            bounds={'sw': leg['start_location'], 'ne': leg['end_location']}
                        )
                        routes_list.append(route_alt)
                        logger.info(
                            f"[{request_id}] Route {route_alt.route_id}: {route_alt.distance_miles:.1f} mi, "
                            f"{route_alt.duration_seconds/3600:.1f} hrs"
                        )

                    except Exception as e:
                        logger.warning(f"[{request_id}] Failed to parse route {i}: {e}")

                if not routes_list:
                    raise Exception("No valid routes returned from Google API")

                # Cache ALL alternatives (each with unique key)
                cls._cache_routes(base_key, routes_list)

                logger.info(f"[{request_id}] Successfully got {len(routes_list)} alternative routes")
                return routes_list

            except Exception as e:
                logger.error(f"[{request_id}] Google Directions API error: {e}", exc_info=True)
                raise

        result = RequestLockManager.coalesce_request(
            lock_key=lock_key,
            result_key=cache_key,
            compute_fn=compute_routes,
            cache_ttl=CACHE_TTL_ROUTE
        )

        return result if isinstance(result, list) else []

    @classmethod
    def _get_cached_alternatives(cls, base_key: str) -> Optional[List[RouteAlternative]]:
        """Retrieve all cached route alternatives for a base key."""
        try:
            now = timezone.now()

            # Try single-route cache entry (backward compat)
            single = RouteCache.objects.filter(
                cache_key=base_key,
                is_valid=True,
                expires_at__gt=now
            ).first()

            if single:
                # Lightweight access tracking (best-effort, single query)
                try:
                    RouteCache.objects.filter(
                        cache_key=base_key, is_valid=True
                    ).update(access_count=F('access_count') + 1)
                except Exception:
                    pass
                return [
                    RouteAlternative(
                        route_id='route_a',
                        distance_miles=single.total_distance_miles,
                        duration_seconds=single.google_duration_secs,
                        polyline_encoded=single.google_polyline,
                        bounds={
                            'sw': {'lat': single.start_lat, 'lng': single.start_lon},
                            'ne': {'lat': single.end_lat, 'lng': single.end_lon}
                        }
                    )
                ]

            # Try multi-route cache entries (alt_N keys)
            prefix = f"{base_key}:alt_"
            multi = list(RouteCache.objects.filter(
                cache_key__startswith=prefix,
                is_valid=True,
                expires_at__gt=now
            ).order_by('cache_key').all())

            if multi:
                routes = []
                for i, entry in enumerate(multi):
                    routes.append(RouteAlternative(
                        route_id=f"route_{chr(97 + i)}",
                        distance_miles=entry.total_distance_miles,
                        duration_seconds=entry.google_duration_secs,
                        polyline_encoded=entry.google_polyline,
                        bounds={
                            'sw': {'lat': entry.start_lat, 'lng': entry.start_lon},
                            'ne': {'lat': entry.end_lat, 'lng': entry.end_lon}
                        }
                    ))
                logger.info(f"Retrieved {len(routes)} cached alternatives from database")
                return routes

        except Exception as e:
            logger.warning(f"Route cache lookup failed: {e}")

        return None

    @classmethod
    def _cache_routes(cls, base_key: str, routes: List[RouteAlternative]):
        """Persist route alternatives in RouteCache database."""
        expiry = timezone.now() + timedelta(hours=24)
        try:
            for i, route in enumerate(routes):
                if len(routes) == 1:
                    route_key = base_key
                else:
                    route_key = f"{base_key}:alt_{i}"

                decoded_coords = polyline.decode(route.polyline_encoded)
                defaults = dict(
                    start_address=f"{route.bounds['sw']['lat']:.6f}, {route.bounds['sw']['lng']:.6f}",
                    end_address=f"{route.bounds['ne']['lat']:.6f}, {route.bounds['ne']['lng']:.6f}",
                    start_lat=route.bounds['sw']['lat'],
                    start_lon=route.bounds['sw']['lng'],
                    end_lat=route.bounds['ne']['lat'],
                    end_lon=route.bounds['ne']['lng'],
                    route_polyline=LineString(decoded_coords),
                    route_geography=LineString(decoded_coords),
                    total_distance_miles=route.distance_miles,
                    google_polyline=route.polyline_encoded,
                    google_duration_secs=route.duration_seconds,
                    google_api_cost=Decimal('0.01'),
                    expires_at=expiry,
                    is_valid=True,
                    computation_time_ms=0,
                )
                RouteCache.objects.update_or_create(
                    cache_key=route_key,
                    defaults=defaults,
                )

            logger.info(f"Cached {len(routes)} route(s) for 24 hours")
        except Exception as e:
            logger.warning(f"Failed to cache routes: {e}")
