"""Geocoding service using Google Geocoding API with caching and request coalescing."""
import logging
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Optional, Tuple

import requests
from django.core.cache import cache
from django.utils import timezone

from .cache_utils import EnhancedCacheKeyGenerator, RequestLockManager, RouteNormalizer
from .constants import GOOGLE_API_KEY, GOOGLE_GEOCODING_ENDPOINT, CACHE_TTL_GEOCODE
from .models import GeocodeFailure
from .route_geometry import _fast_distance_miles

logger = logging.getLogger(__name__)


@dataclass
class Location:
    """Geographic coordinate."""
    latitude: float
    longitude: float

    def as_tuple(self) -> Tuple[float, float]:
        return (self.latitude, self.longitude)

    def distance_to(self, other: 'Location') -> float:
        """Distance in miles using fast haversine."""
        return _fast_distance_miles(self.latitude, self.longitude, other.latitude, other.longitude)


class GeocodingService:
    """Geocode addresses to coordinates with caching and request coalescing."""

    @staticmethod
    def geocode(address: str, request_id: str = '') -> Optional[Location]:
        """Geocode address to coordinates with caching and request coalescing."""
        cache_key = EnhancedCacheKeyGenerator.geocode_key(address)
        lock_key = EnhancedCacheKeyGenerator.lock_key(cache_key)

        cached = cache.get(cache_key)
        if cached:
            logger.debug(f"[{request_id}] Geocoding cache HIT: {address}")
            if isinstance(cached, dict):
                return Location(**cached)
            elif isinstance(cached, Location):
                return cached
            return None

        def compute_geocode():
            logger.info(f"[{request_id}] Geocoding address: {address}")
            try:
                response = requests.get(
                    GOOGLE_GEOCODING_ENDPOINT,
                    params={
                        'address': address,
                        'key': GOOGLE_API_KEY,
                        'region': 'us',
                        'components': 'country:US'
                    },
                    timeout=(5, 10)
                )
                response.raise_for_status()

                data = response.json()
                if data.get('status') != 'OK' or not data.get('results'):
                    raise ValueError(f"Geocoding failed: {data.get('status')}")

                result = data['results'][0]
                geometry = result['geometry']['location']
                location = Location(
                    latitude=geometry['lat'],
                    longitude=geometry['lng']
                )

                cache.set(cache_key, asdict(location), CACHE_TTL_GEOCODE)
                logger.info(f"[{request_id}] Geocoded {address} -> ({location.latitude:.6f}, {location.longitude:.6f})")
                return location

            except Exception as e:
                logger.error(f"[{request_id}] Geocoding error for '{address}': {e}")
                GeocodeFailure.objects.create(
                    original_address=address,
                    city=address.split(',')[0] if ',' in address else '',
                    state='US',
                    failure_reason=str(e),
                    google_error_message=str(e),
                    next_retry_at=timezone.now() + timedelta(hours=1)
                )
                return None

        result = RequestLockManager.coalesce_request(
            lock_key=lock_key,
            result_key=cache_key,
            compute_fn=compute_geocode,
            cache_ttl=CACHE_TTL_GEOCODE
        )

        if result is None:
            return None
        elif isinstance(result, dict):
            return Location(**result)
        elif isinstance(result, Location):
            return result
        return None
