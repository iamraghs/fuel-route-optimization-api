"""API Views for Fuel Route Optimization."""
import logging
import hashlib
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.utils import timezone

from .serializers import (
    FuelOptimizationRequestSerializer,
    FuelOptimizationResponseSerializer
)
from .engine import FuelRouteOptimizationEngine
from .models import RouteRequest

logger = logging.getLogger(__name__)


@api_view(['POST'])
def optimize_fuel_route(request):
    """
    Optimize route for minimal fuel cost.

    POST /route/fuel-optimization
    Body: {"start": "City, State"|{lat,lng}, "finish": "City, State"|{lat,lng}}

    Accepts address strings or lat/lng dicts. Returns selected route with fuel stops,
    route comparison, and trip summary.
    """

    # Validate request
    serializer = FuelOptimizationRequestSerializer(data=request.data)
    if not serializer.is_valid():
        # Structured validation error: identify which field failed
        errors = serializer.errors
        error_code = "INVALID_REQUEST"
        message = "Invalid request"
        if 'finish' in errors:
            error_code = "INVALID_DESTINATION"
            message = errors['finish'][0] if isinstance(errors['finish'], list) else str(errors['finish'])
        elif 'start' in errors:
            error_code = "INVALID_ORIGIN"
            message = errors['start'][0] if isinstance(errors['start'], list) else str(errors['start'])
        return Response(
            {
                "status": "error",
                "error_code": error_code,
                "message": message,
            },
            status=status.HTTP_400_BAD_REQUEST
        )
    
    start_input = serializer.validated_data['start']
    end_input = serializer.validated_data['finish']
    
    try:
        result = FuelRouteOptimizationEngine.optimize(start_input, end_input)

        try:
            RouteRequest.objects.create(
                request_id=result.get('request_id', 'unknown'),
                start_address=str(start_input)[:255],
                end_address=str(end_input)[:255],
                total_distance_miles=result.get('trip_summary', {}).get('total_distance_miles', 0),
                total_fuel_cost=result.get('trip_summary', {}).get('total_fuel_cost', 0),
                fuel_stop_count=len(result.get('fuel_stops', [])),
                optimization_time_ms=result.get('optimization_time_ms', 0),
                cache_hit=result.get('_cache_hit', False),
                google_api_calls=1,
                client_ip=request.META.get('REMOTE_ADDR'),
                completed_at=timezone.now()
            )
        except Exception as e:
            logger.warning(f"Failed to log route request: {e}")
        
        if result.get('status') == 'unreachable':
            http_response = Response(
                {k: v for k, v in result.items() if not k.startswith('_')},
                status=status.HTTP_200_OK
            )
        else:
            response_serializer = FuelOptimizationResponseSerializer(result)
            http_response = Response(response_serializer.data, status=status.HTTP_200_OK)

        http_response['Cache-Control'] = 'max-age=43200, public'
        http_response['ETag'] = hashlib.md5(str(result).encode()).hexdigest()[:16]
        
        return http_response
        
    except ValueError as e:
        logger.error(f"Validation error: {e}")
        # Geocoding failures get a structured error code
        msg = str(e)
        error_code = "INVALID_REQUEST"
        if "could not be resolved" in msg.lower() or "geocode" in msg.lower():
            error_code = "INVALID_DESTINATION" if "destination" in msg.lower() or "end" in msg.lower() else "INVALID_ORIGIN"
        return Response(
            {
                "status": "error",
                "error_code": error_code,
                "message": msg,
            },
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Optimization error: {e}")
        return Response(
            {'error': 'Optimization failed', 'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
