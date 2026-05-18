"""Multi-level cache for sub-second API responses."""
import hashlib
import json
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)


class UltraFastCache:
    """Cache for optimization results, routes, and geocoding."""

    GEOCODE_TTL = 86400 * 7
    ROUTE_TTL = 86400 * 14
    OPTIMIZATION_TTL = 3600 * 12

    @staticmethod
    def _normalize(val):
        if isinstance(val, dict):
            return json.dumps(val, sort_keys=True, default=str)
        return str(val).lower().strip()

    @staticmethod
    def make_optimization_key(start_input, end_input, route_id=None, price_version=0):
        """Create deterministic cache key including price version."""
        key_str = f"opt:v4:{UltraFastCache._normalize(start_input)}:{UltraFastCache._normalize(end_input)}:pv{price_version}"
        if route_id:
            key_str += f":{route_id}"
        return hashlib.md5(key_str.encode()).hexdigest()

    @staticmethod
    def get_cached_optimization(start_input, end_input, price_version=0, route_id=None):
        """Get cached optimization result if available."""
        key = UltraFastCache.make_optimization_key(start_input, end_input, route_id, price_version)
        result = cache.get(key)
        if result:
            logger.info(f"Cache HIT (optimization): {key[:8]}...")
        return result

    @staticmethod
    def cache_optimization(start_input, end_input, result, price_version=0, route_id=None, ttl=None):
        """Cache optimization result."""
        if ttl is None:
            ttl = UltraFastCache.OPTIMIZATION_TTL
        key = UltraFastCache.make_optimization_key(start_input, end_input, route_id, price_version)
        cache.set(key, result, ttl)
        logger.info(f"Cached (optimization): {key[:8]}... for {ttl}s")
