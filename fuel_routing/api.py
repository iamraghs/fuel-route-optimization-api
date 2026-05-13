"""API Views for Fuel Route Optimization."""
import logging
import hashlib
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.utils.decorators import decorator_from_middleware_with_args
from django.middleware.cache import UpdateCacheMiddleware

from .serializers import (
    FuelOptimizationRequestSerializer,
    FuelOptimizationResponseSerializer
)
from .optimize import FuelRouteOptimizationEngine
from .models import RouteRequest

logger = logging.getLogger(__name__)


@api_view(['POST'])
def optimize_fuel_route(request):
    """
    Optimize route for minimal fuel cost.
    
    POST /route/fuel-optimization
    
    Request:
    {
        "start": "Los Angeles, CA",
        "finish": "New York, NY"
    }
    
    or
    
    {
        "start": {"lat": 34.0522, "lng": -118.2437},
        "finish": {"lat": 40.7128, "lng": -74.0060}
    }
    
    Response:
    {
        "selected_route": {
            "route_id": "route_b",
            "distance_miles": 1480,
            "estimated_drive_time": "22h",
            "total_fuel_cost": 510,
            "total_fuel_consumed_gallons": 148
        },
        "route_comparison": [
            {"route_id": "route_a", "distance_miles": 1400, "fuel_cost": 620, "stops": 3},
            {"route_id": "route_b", "distance_miles": 1480, "fuel_cost": 510, "stops": 2}
        ],
        "fuel_stops": [
            {
                "station_name": "Love's",
                "city": "Oklahoma City",
                "state": "OK",
                "mile_marker": 430,
                "fuel_price": 3.45,
                "gallons_to_buy": 22,
                "fuel_cost": 75.90,
                "remaining_range_after_fill": 500
            },
            ...
        ],
        "total_distance_miles": 1480,
        "total_fuel_cost": 510,
        "total_fuel_consumed_gallons": 148,
        "route_polyline": "encoded_polyline_here",
        "optimization_time_ms": 87,
        "request_id": "a1b2c3d4"
    }
    
    Business Logic:
    1. Accept start/end locations (address string or lat/lng)
    2. Get 2 alternative routes from Google Routes API
    3. Query fuel stations in corridor (PostGIS)
    4. Optimize fuel stops for each route (Greedy + Lookahead)
    5. Compare routes by total fuel cost
    6. Select route with LOWEST TOTAL COST (not shortest distance)
    7. Return selected route with detailed fuel stops
    """
    
    # Step 1: Validate request
    serializer = FuelOptimizationRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    start_input = serializer.validated_data['start']
    end_input = serializer.validated_data['finish']
    
    try:
        # Step 2: Run optimization
        result = FuelRouteOptimizationEngine.optimize(start_input, end_input)
        
        # Step 3: Log request (append-only)
        try:
            RouteRequest.objects.create(
                request_id=result.get('request_id', 'unknown'),
                start_address=str(start_input)[:255],
                end_address=str(end_input)[:255],
                total_distance_miles=result.get('total_distance_miles', 0),
                total_fuel_cost=result.get('total_fuel_cost', 0),
                fuel_stop_count=len(result.get('fuel_stops', [])),
                optimization_time_ms=result.get('optimization_time_ms', 0),
                cache_hit=False,  # New request
                google_api_calls=1,  # Called Google Routes API
                client_ip=request.META.get('REMOTE_ADDR'),
                completed_at=timezone.now()
            )
        except Exception as e:
            logger.warning(f"Failed to log route request: {e}")
        
        # Step 4: Return response with cache headers for 12 hours
        # ⚡ PERFORMANCE: Cache responses for faster repeated requests
        response_serializer = FuelOptimizationResponseSerializer(result)
        http_response = Response(response_serializer.data, status=status.HTTP_200_OK)
        
        # Set cache headers (12 hours = 43200 seconds)
        # This allows browsers, proxies, and CDNs to cache the response
        http_response['Cache-Control'] = 'max-age=43200, public'  # 12 hours
        http_response['ETag'] = hashlib.md5(str(result).encode()).hexdigest()[:16]
        
        return http_response
        
    except ValueError as e:
        logger.error(f"Validation error: {e}")
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
    except Exception as e:
        logger.error(f"Optimization error: {e}")
        return Response(
            {'error': 'Optimization failed', 'detail': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
