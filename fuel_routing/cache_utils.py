"""
Production-Grade Cache Utilities with Request Coalescing and Normalization.

Enhancements over base cache.py:
  • Unified route normalization (addresses + coordinates)
  • Distributed request locking to prevent duplicate computation
  • Request coalescing for concurrent identical requests
  • Standardized cache key generation
  • Atomic cache operations
  • Proper error handling with fallbacks
"""

import hashlib
import logging
import time
from typing import Optional, Dict, Any, Callable, TypeVar
from decimal import Decimal
from datetime import datetime, timedelta
from functools import wraps
from threading import Lock

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

T = TypeVar('T')


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
    def optimization_key(cls, start_lat: float, start_lon: float, end_lat: float, end_lon: float, price_version: int = 0) -> str:
        """Cache key for optimization result (price-aware)."""
        start_norm = RouteNormalizer.normalize_coordinates(start_lat, start_lon)
        end_norm = RouteNormalizer.normalize_coordinates(end_lat, end_lon)
        hash_val = cls._make_hash(*start_norm, *end_norm, price_version)
        return f"{cls.PREFIX}:optimization:{cls.VERSION}:{hash_val}"
    
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
        lock_value = f"{time.time()}:{id(Lock())}"
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


# ============================================================================
# CACHE DECORATORS
# ============================================================================

def cached(ttl: int = 3600, key_fn: Optional[Callable] = None):
    """
    Decorator to cache function results.
    
    Usage:
        @cached(ttl=3600, key_fn=lambda start, end: f"optimize:{start}:{end}")
        def expensive_function(start, end):
            return result
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # Generate cache key
            if key_fn:
                cache_key = key_fn(*args, **kwargs)
            else:
                # Default: use function name + args hash
                args_str = "|".join(str(arg) for arg in args)
                kwargs_str = "|".join(f"{k}:{v}" for k, v in sorted(kwargs.items()))
                combined = f"{func.__name__}:{args_str}:{kwargs_str}"
                cache_key = hashlib.md5(combined.encode()).hexdigest()
            
            # Try to get from cache
            result = cache.get(cache_key)
            if result is not None:
                logger.debug(f"Cache HIT: {func.__name__}")
                return result
            
            # Compute and cache
            result = func(*args, **kwargs)
            cache.set(cache_key, result, ttl)
            return result
        
        return wrapper
    
    return decorator


# ============================================================================
# VERSION MANAGEMENT (for price-aware caching)
# ============================================================================

class CacheVersionManager:
    """Manage cache versioning for price updates."""
    
    PRICE_VERSION_KEY = "price:version"
    
    @classmethod
    def get_price_version(cls) -> int:
        """Get current price version."""
        version = cache.get(cls.PRICE_VERSION_KEY, 0)
        return version
    
    @classmethod
    def increment_price_version(cls):
        """Increment price version (invalidates price-aware caches)."""
        current = cls.get_price_version()
        cache.set(cls.PRICE_VERSION_KEY, current + 1, None)  # None = persist forever
        logger.info(f"Price version incremented to {current + 1}")
    
    @classmethod
    def get_optimization_key_with_version(
        cls,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float
    ) -> str:
        """Get optimization cache key with current price version."""
        price_version = cls.get_price_version()
        return EnhancedCacheKeyGenerator.optimization_key(
            start_lat, start_lon, end_lat, end_lon, price_version
        )
