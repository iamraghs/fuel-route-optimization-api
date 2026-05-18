"""
Unified cache service for optimization results.

Provides a single, consistent cache layer for final optimization responses.
Uses normalized, versioned keys (price-version-aware) for deterministic caching.

Key format:  fuel_routing:optimization:v1:{input_hash}:pv{price_version}

Backward compatibility:
  - Falls back to read legacy UltraFastCache keys on first miss
  - Writes both new-format and legacy keys during transition
"""
import hashlib
import logging
from typing import Any, Dict, Optional, Callable, TypeVar

from django.core.cache import cache

from .cache_utils import (
    EnhancedCacheKeyGenerator,
    RequestLockManager,
    RouteNormalizer,
)
from .ultra_cache import UltraFastCache

logger = logging.getLogger(__name__)

T = TypeVar("T")

PREFIX = "fuel_routing"
VERSION = "v1"
OPTIMIZATION_TTL = 3600  # 1 hour (default, matches CACHE_TTL['OPTIMIZATION'])


def _make_optimization_cache_key(
    start_input: str | Dict[str, float],
    end_input: str | Dict[str, float],
    price_version: int,
) -> str:
    """Generate deterministic, normalized cache key for optimization results.

    Normalizes both address strings and coordinate dicts so that
    semantically identical inputs produce the same cache key.
    """
    raw_key = (
        f"{PREFIX}:optimization:{VERSION}:"
        f"{_normalize_cache_input(start_input)}|{_normalize_cache_input(end_input)}"
        f":pv{price_version}"
    )
    return hashlib.md5(raw_key.encode()).hexdigest()


def _normalize_cache_input(val: str | Dict[str, float]) -> str:
    """Normalize cache input (address string or coordinate dict) to canonical form."""
    if isinstance(val, dict):
        lat = val.get("lat") or val.get("latitude", 0)
        lng = val.get("lng") or val.get("longitude", 0)
        return f"{float(lat):.4f},{float(lng):.4f}"
    return RouteNormalizer.normalize_address(val)


def _redis_key(cache_hash: str) -> str:
    return f"{PREFIX}:optimization:{VERSION}:{cache_hash}"


def get_cached_optimization(
    start_input: str | Dict[str, float],
    end_input: str | Dict[str, float],
    price_version: int,
) -> Optional[Dict[str, Any]]:
    """Get cached optimization result.

    Checks unified cache first (new key format).
    Falls back to legacy UltraFastCache for backward compatibility.
    Returns None on cache miss.
    """
    cache_hash = _make_optimization_cache_key(start_input, end_input, price_version)
    redis_key = _redis_key(cache_hash)

    try:
        result = cache.get(redis_key)
        if result is not None:
            logger.debug(f"Optimization cache HIT (unified): {redis_key[:24]}...")
            # Ensure _cache_hit is set for the response
            if isinstance(result, dict):
                result["_cache_hit"] = True
            return result
    except Exception as e:
        logger.warning(f"Unified cache read failed: {e}")

    # Backward compat: try legacy UltraFastCache key
    try:
        legacy = UltraFastCache.get_cached_optimization(
            start_input, end_input, price_version=price_version
        )
        if legacy is not None:
            logger.info("Optimization cache HIT (legacy UltraFastCache)")
            # Migrate to new key format
            set_cached_optimization(start_input, end_input, legacy, price_version)
            if isinstance(legacy, dict):
                legacy["_cache_hit"] = True
            return legacy
    except Exception:
        pass

    return None


def set_cached_optimization(
    start_input: str | Dict[str, float],
    end_input: str | Dict[str, float],
    result: Dict[str, Any],
    price_version: int,
    ttl: int = OPTIMIZATION_TTL,
):
    """Cache optimization result with unified key format.

    Writes to both new unified key and legacy UltraFastCache key
    during transition period for backward compatibility.
    """
    cache_hash = _make_optimization_cache_key(start_input, end_input, price_version)
    redis_key = _redis_key(cache_hash)

    # Strip transient fields before caching
    to_cache = dict(result)
    to_cache.pop("_cache_hit", None)

    try:
        cache.set(redis_key, to_cache, ttl)
        logger.debug(f"Optimization cached (unified): {redis_key[:24]}... TTL={ttl}s")
    except Exception as e:
        logger.warning(f"Unified cache write failed: {e}")

    # Legacy write for backward compat
    try:
        UltraFastCache.cache_optimization(
            start_input, end_input, to_cache, price_version=price_version, ttl=ttl
        )
    except Exception:
        pass


def get_or_compute_optimization(
    start_input: str | Dict[str, float],
    end_input: str | Dict[str, float],
    price_version: int,
    compute_fn: Callable[[], Dict[str, Any]],
    ttl: int = OPTIMIZATION_TTL,
) -> Dict[str, Any]:
    """Get cached optimization or compute + cache with request coalescing.

    Prevents duplicate optimization computation across concurrent requests
    via Redis-based locking.
    """
    # Quick check outside lock (fast path)
    cached = get_cached_optimization(start_input, end_input, price_version)
    if cached is not None:
        return cached

    cache_hash = _make_optimization_cache_key(start_input, end_input, price_version)
    redis_key = _redis_key(cache_hash)
    lock_key = f"{redis_key}:lock"

    def compute_and_cache():
        logger.info(f"Computing optimization (coalescing): {redis_key[:24]}...")
        result = compute_fn()
        set_cached_optimization(start_input, end_input, result, price_version, ttl)
        return result

    result = RequestLockManager.coalesce_request(
        lock_key=lock_key,
        result_key=redis_key,
        compute_fn=compute_and_cache,
        cache_ttl=ttl,
    )

    if result is None:
        # Fallback: compute without caching guard
        logger.warning("Optimization coalescing timeout, computing directly")
        result = compute_fn()

    if isinstance(result, dict):
        result["_cache_hit"] = result.get("_cache_hit", False)
    return result
