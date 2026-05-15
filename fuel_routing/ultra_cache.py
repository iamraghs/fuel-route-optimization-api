"""Ultra-aggressive caching for <1 second API responses."""
import hashlib
import json
import logging
from django.core.cache import cache
from datetime import timedelta

logger = logging.getLogger(__name__)

class UltraFastCache:
    """Multi-level cache for sub-second responses."""
    
    # Cache TTL levels
    GEOCODE_TTL = 86400 * 7  # 7 days (static data)
    ROUTE_TTL = 86400 * 14  # 14 days (routes rarely change)
    OPTIMIZATION_TTL = 3600 * 12  # 12 hours (fuel prices update daily)
    
    @staticmethod
    def make_optimization_key(start_input, end_input, route_id=None):
        """Create cache key for optimization request."""
        # Normalize dict inputs for deterministic key ordering
        def normalize_input(val):
            if isinstance(val, dict):
                return json.dumps(val, sort_keys=True, default=str)
            return str(val).lower().strip()

        key_str = f"opt:v3:{normalize_input(start_input)}:{normalize_input(end_input)}"
        if route_id:
            key_str += f":{route_id}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    @staticmethod
    def get_cached_optimization(start_input, end_input, route_id=None):
        """Get cached optimization result if available."""
        key = UltraFastCache.make_optimization_key(start_input, end_input, route_id)
        result = cache.get(key)
        if result:
            logger.info(f"🚀 CACHE HIT (optimization): {key[:8]}...")
            return result
        return None
    
    @staticmethod
    def cache_optimization(start_input, end_input, result, route_id=None, ttl=None):
        """Cache optimization result."""
        if ttl is None:
            ttl = UltraFastCache.OPTIMIZATION_TTL
        
        key = UltraFastCache.make_optimization_key(start_input, end_input, route_id)
        cache.set(key, result, ttl)
        logger.info(f"💾 CACHED (optimization): {key[:8]}... for {ttl}s")
    
    @staticmethod
    def get_cached_route(start_loc, end_loc):
        """Get cached route geometry if available."""
        key = f"route:v3:{str(start_loc)}:{str(end_loc)}"
        result = cache.get(key)
        if result:
            logger.info(f"🚀 CACHE HIT (route): {key[:8]}...")
            return result
        return None

    @staticmethod
    def cache_route(start_loc, end_loc, result, ttl=None):
        """Cache route geometry."""
        if ttl is None:
            ttl = UltraFastCache.ROUTE_TTL

        key = f"route:v3:{str(start_loc)}:{str(end_loc)}"
        cache.set(key, result, ttl)
        logger.info(f"💾 CACHED (route): {key[:8]}... for {ttl}s")

    @staticmethod
    def get_cached_geocode(address):
        """Get cached geocoding result."""
        key = f"geocode:v3:{address.lower().strip()}"
        result = cache.get(key)
        if result:
            logger.info(f"🚀 CACHE HIT (geocode): {key[:8]}...")
            return result
        return None

    @staticmethod
    def cache_geocode(address, result, ttl=None):
        """Cache geocoding result."""
        if ttl is None:
            ttl = UltraFastCache.GEOCODE_TTL

        key = f"geocode:v3:{address.lower().strip()}"
        cache.set(key, result, ttl)
        logger.info(f"💾 CACHED (geocode): {key[:8]}... for {ttl}s")
    
    @staticmethod
    def clear_optimization_cache(start_input=None, end_input=None):
        """Clear optimization cache (e.g., when fuel prices update)."""
        if start_input and end_input:
            key = UltraFastCache.make_optimization_key(start_input, end_input)
            cache.delete(key)
            logger.info(f"Cleared optimization cache: {start_input} → {end_input}")
        else:
            # Clear all optimization caches
            logger.warning("Clearing ALL optimization caches (expensive operation)")
            # In production, implement selective cache invalidation via pubsub
