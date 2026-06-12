"""
Unified cache service for optimization results.

Provides a single, consistent cache layer for final optimization responses.
Uses normalized, versioned keys (price-version-aware) for deterministic caching.

Key format:  fuel_routing:optimization:v1:{input_hash}:pv{price_version}
"""
import hashlib
import logging
from typing import Any, Dict, Optional

from django.core.cache import cache

from .cache_utils import RouteNormalizer

logger = logging.getLogger(__name__)

PREFIX = "fuel_routing"
VERSION = "v1"
OPTIMIZATION_TTL = 3600  # 1 hour (matches CACHE_TTL['OPTIMIZATION'])


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
    """Get cached optimization result from unified cache."""
    cache_hash = _make_optimization_cache_key(start_input, end_input, price_version)
    redis_key = _redis_key(cache_hash)

    try:
        result = cache.get(redis_key)
        if result is not None:
            logger.debug(f"Optimization cache HIT (unified): {redis_key[:24]}...")
            if isinstance(result, dict):
                result["_cache_hit"] = True
            return result
    except Exception as e:
        logger.warning(f"Unified cache read failed: {e}")

    return None


def set_cached_optimization(
    start_input: str | Dict[str, float],
    end_input: str | Dict[str, float],
    result: Dict[str, Any],
    price_version: int,
    ttl: int = OPTIMIZATION_TTL,
):
    """Cache optimization result with unified key format."""
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



