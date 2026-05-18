"""
Production-Grade Cache Utilities with Request Coalescing, Geometry Caching, and Corridor Caching.

Enhancements:
  • Unified route normalization (addresses + coordinates)
  • Distributed request locking to prevent duplicate computation
  • Request coalescing for concurrent identical requests
  • Standardized cache key generation
  • Atomic cache operations
  • Geometry cache: in-process LRU for decoded polylines + cumulative distances
  • Corridor station cache: Redis-backed filtered station ID sets per route
  • Proper error handling with fallbacks
"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Dict, Any, Callable, TypeVar, List, Tuple

from django.core.cache import cache

logger = logging.getLogger(__name__)

T = TypeVar('T')


# ============================================================================
# GEOMETRY CACHE (in-process LRU for decoded polyline + cumulative distances)
# ============================================================================

class GeometryCache:
    """In-process LRU cache for decoded polyline coordinates and cumulative distances.

    Avoids repeated polyline decode and cumulative distance computation.
    Bounded at MAX_SIZE entries. Thread-safe for CPython (GIL protects dict ops).
    """
    _cache: OrderedDict = OrderedDict()
    MAX_SIZE = 200

    @classmethod
    def make_key(cls, polyline_encoded: str) -> str:
        """Generate deterministic key from encoded polyline."""
        return hashlib.md5(polyline_encoded.encode()).hexdigest()

    @classmethod
    def get(cls, polyline_encoded: str) -> Optional[Tuple[List[Tuple[float, float]], List[float], List[Tuple[float, float]], List[float]]]:
        """Get cached geometry data. Returns None if not cached."""
        key = cls.make_key(polyline_encoded)
        if key in cls._cache:
            cls._cache.move_to_end(key)
            return cls._cache[key]
        return None

    @classmethod
    def set(cls, polyline_encoded: str, coords: List[Tuple[float, float]], cum_dist: List[float],
            sampled_coords: List[Tuple[float, float]], sampled_cum_dist: List[float]):
        """Store geometry data in cache. Evicts oldest if over MAX_SIZE."""
        if not polyline_encoded:
            return
        key = cls.make_key(polyline_encoded)
        cls._cache[key] = (coords, cum_dist, sampled_coords, sampled_cum_dist)
        cls._cache.move_to_end(key)
        if len(cls._cache) > cls.MAX_SIZE:
            cls._cache.popitem(last=False)

    @classmethod
    def clear(cls):
        """Clear all cached geometry."""
        cls._cache.clear()

    @classmethod
    def size(cls) -> int:
        return len(cls._cache)


# ============================================================================
# CORRIDOR STATION CACHE (Redis-backed filtered station sets per route)
# ============================================================================

CORRIDOR_CACHE_PREFIX = "fuel_routing:corridor:v1"
CORRIDOR_CACHE_TTL = 3600  # 1 hour (station sets stable; price changes don't affect which stations exist)


class CorridorStationCache:
    """Redis-backed cache for corridor-filtered station ID sets per route.

    Avoids repeated polyline-based corridor filtering for the same route.
    Stores only OPIS IDs (not full station data), so price changes don't invalidate.
    """

    @staticmethod
    def _make_key(route_id: str, buffer_miles: float) -> str:
        return f"{CORRIDOR_CACHE_PREFIX}:{route_id}:buf{int(buffer_miles)}"

    @staticmethod
    def get(route_id: str, buffer_miles: float) -> Optional[List[int]]:
        """Get cached station OPIS IDs for a route corridor."""
        key = CorridorStationCache._make_key(route_id, buffer_miles)
        try:
            result = cache.get(key)
            if result is not None:
                logger.debug(f"Corridor cache HIT: {key}")
                return result
        except Exception:
            pass
        return None

    @staticmethod
    def set(route_id: str, buffer_miles: float, opis_ids: List[int]):
        """Cache station OPIS IDs for a route corridor."""
        if not opis_ids:
            return
        key = CorridorStationCache._make_key(route_id, buffer_miles)
        try:
            cache.set(key, opis_ids, CORRIDOR_CACHE_TTL)
            logger.debug(f"Cached {len(opis_ids)} corridor station IDs: {key}")
        except Exception as e:
            logger.warning(f"Failed to cache corridor stations: {e}")

    @staticmethod
    def get_or_compute(
        route_id: str,
        buffer_miles: float,
        compute_fn: Callable[[], List[int]]
    ) -> List[int]:
        """Get cached corridor station IDs or compute and cache."""
        cached = CorridorStationCache.get(route_id, buffer_miles)
        if cached is not None:
            return cached
        opis_ids = compute_fn()
        CorridorStationCache.set(route_id, buffer_miles, opis_ids)
        return opis_ids


# ============================================================================
# ROUTE NORMALIZATION (standardize address/coordinate inputs)
# ============================================================================

class RouteNormalizer:
    """Normalize route inputs for consistent caching."""
    
    @staticmethod
    def normalize_address(address: str) -> str:
        """
        Normalize address string for consistent caching.
        
        Examples:
          "Los Angeles, CA" → "los angeles, ca"
          "Los Angeles, California" → "los angeles, ca"
          "  Los Angeles  " → "los angeles"
        """
        if not isinstance(address, str):
            return str(address)
        
        # Strip whitespace and lowercase
        normalized = address.strip().lower()
        
        # Expand common state abbreviations
        state_map = {
            'california': 'ca', 'texas': 'tx', 'florida': 'fl',
            'new york': 'ny', 'pennsylvania': 'pa', 'illinois': 'il',
        }
        
        for full_name, abbrev in state_map.items():
            normalized = normalized.replace(full_name, abbrev)
        
        # Remove extra spaces
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    @staticmethod
    def normalize_coordinates(lat: float, lon: float) -> tuple[str, str]:
        """
        Normalize coordinates to fixed precision for caching.
        
        Rounds to 4 decimal places (~11 meters precision)
        """
        return (f"{lat:.4f}", f"{lon:.4f}")


# ============================================================================
# CACHE KEY GENERATION (consistent, versioned keys)
# ============================================================================

class EnhancedCacheKeyGenerator:
    """Generate consistent, versioned cache keys."""
    
    PREFIX = "fuel_routing"
    VERSION = "v1"
    
    @staticmethod
    def _make_hash(*args) -> str:
        """Create MD5 hash from arguments."""
        data = "|".join(str(arg) for arg in args)
        return hashlib.md5(data.encode()).hexdigest()
    
    @classmethod
    def geocode_key(cls, address: str) -> str:
        """Cache key for geocoded address."""
        normalized = RouteNormalizer.normalize_address(address)
        hash_val = cls._make_hash(normalized)
        return f"{cls.PREFIX}:geocode:{cls.VERSION}:{hash_val}"
    
    @classmethod
    def route_key(cls, start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> str:
        """Cache key for route between two coordinates."""
        start_norm = RouteNormalizer.normalize_coordinates(start_lat, start_lon)
        end_norm = RouteNormalizer.normalize_coordinates(end_lat, end_lon)
        hash_val = cls._make_hash(*start_norm, *end_norm)
        return f"{cls.PREFIX}:route:{cls.VERSION}:{hash_val}"
    
    @classmethod
    def lock_key(cls, cache_key: str) -> str:
        """Generate a lock key for preventing duplicate computation."""
        return f"{cache_key}:lock"


# ============================================================================
# REQUEST LOCKING (prevent duplicate API calls)
# ============================================================================

class RequestLockManager:
    """Manage distributed locks to prevent duplicate requests."""
    
    LOCK_TIMEOUT = 30  # seconds
    POLL_INTERVAL = 0.1  # seconds
    MAX_WAIT = 5  # seconds to wait for lock release
    
    @classmethod
    def acquire_lock(cls, lock_key: str, timeout: int = LOCK_TIMEOUT) -> bool:
        """
        Attempt to acquire a lock.
        
        Returns True if lock acquired, False if already locked.
        """
        lock_value = f"{time.time()}:{threading.get_ident()}"
        result = cache.add(lock_key, lock_value, timeout)
        return result
    
    @classmethod
    def wait_for_result(cls, lock_key: str, result_key: str, max_wait: int = MAX_WAIT) -> Optional[Any]:
        """
        Wait for another request to finish and return its result.
        
        Args:
            lock_key: The lock key being held by another request
            result_key: The cache key where result will be stored
            max_wait: Maximum seconds to wait
            
        Returns:
            Cached result if available, None if timeout
        """
        elapsed = 0
        while elapsed < max_wait:
            result = cache.get(result_key)
            if result is not None:
                logger.info(f"Got coalesced result for {result_key}")
                return result
            
            time.sleep(cls.POLL_INTERVAL)
            elapsed += cls.POLL_INTERVAL
        
        logger.warning(f"Timeout waiting for result: {result_key}")
        return None
    
    @classmethod
    def release_lock(cls, lock_key: str):
        """Release a lock."""
        cache.delete(lock_key)
    
    @classmethod
    def coalesce_request(
        cls,
        lock_key: str,
        result_key: str,
        compute_fn: Callable[[], T],
        cache_ttl: int
    ) -> Optional[T]:
        """
        Handle request coalescing for expensive computations.
        
        If another request is already computing the same result, wait for it.
        Otherwise, compute and cache the result.
        
        Args:
            lock_key: Key to use for locking
            result_key: Key where result will be cached
            compute_fn: Function to compute if not cached
            cache_ttl: Time-to-live for cached result in seconds
            
        Returns:
            Computed or cached result, or None if timeout
        """
        return AtomicCacheOps.get_or_compute(
            cache_key=result_key,
            compute_fn=compute_fn,
            ttl=cache_ttl,
            lock_key=lock_key
        )


# ============================================================================
# ATOMIC CACHE OPERATIONS
# ============================================================================

class AtomicCacheOps:
    """Atomic cache operations with proper versioning."""
    
    @staticmethod
    def get_or_compute(
        cache_key: str,
        compute_fn: Callable[[], T],
        ttl: int = 3600,
        lock_key: Optional[str] = None
    ) -> T:
        """
        Get from cache or compute atomically.
        
        Features:
          • Request coalescing: if another request is computing, wait for it
          • Prevents thundering herd problem
          • Returns cached result if available
          • Computes and caches if missing
          
        Args:
            cache_key: Key for caching result
            compute_fn: Function to compute if not cached
            ttl: Time-to-live in seconds
            lock_key: Optional lock key (if None, generated from cache_key)
            
        Returns:
            Cached or computed result
        """
        if lock_key is None:
            lock_key = f"{cache_key}:lock"
        
        # Try to get from cache first
        result = cache.get(cache_key)
        if result is not None:
            logger.debug(f"Cache HIT: {cache_key}")
            return result
        
        # Try to acquire lock
        if RequestLockManager.acquire_lock(lock_key):
            try:
                # Check again in case another request cached while we were acquiring lock
                result = cache.get(cache_key)
                if result is not None:
                    return result
                
                # Compute result
                logger.info(f"Computing {cache_key}")
                result = compute_fn()
                
                # Cache result
                cache.set(cache_key, result, ttl)
                
                return result
            finally:
                RequestLockManager.release_lock(lock_key)
        else:
            # Another request is computing, wait for its result
            logger.info(f"Another request computing {cache_key}, waiting...")
            result = RequestLockManager.wait_for_result(lock_key, cache_key)
            
            if result is not None:
                return result
            
            # Timeout - compute ourselves
            logger.warning(f"Lock wait timeout for {cache_key}, computing anyway")
            result = compute_fn()
            cache.set(cache_key, result, ttl)
            return result

