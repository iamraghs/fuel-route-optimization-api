"""Health check and system diagnostics endpoints."""
import logging
import time

from django.db import connection
from django.http import JsonResponse
from django.core.cache import cache

from .engine import _cache_hit_counters, _request_counter
from .models import PriceVersion, FuelStation

logger = logging.getLogger(__name__)



def health_check(request):
    """Lightweight health check returning system status."""
    start = time.time()
    status = "ok"
    checks = {}

    # Database connectivity
    try:
        connection.ensure_connection()
        station_count = FuelStation.objects.count()
        pv = PriceVersion.objects.filter(is_active=True).first()
        checks['database'] = {
            'status': 'ok',
            'stations': station_count,
            'active_price_version': pv.version_number if pv else None,
        }
    except Exception as e:
        status = "degraded"
        checks['database'] = {'status': 'error', 'detail': 'Connection failed'}
        logger.error(f"Health check database failure: {e}")

    # Cache connectivity
    try:
        cache.set('health:ping', time.time(), 10)
        cache.get('health:ping')
        checks['cache'] = {'status': 'ok'}
    except Exception as e:
        status = "degraded"
        checks['cache'] = {'status': 'error', 'detail': 'Connection failed'}
        logger.error(f"Health check cache failure: {e}")

    elapsed_ms = int((time.time() - start) * 1000)

    return JsonResponse({
        'status': status,
        'checks': checks,
        'metrics': {
            'requests_processed': _request_counter,
            'optimization_cache_hits': _cache_hit_counters.get('optimization', 0),
        },
        'response_time_ms': elapsed_ms,
    })
