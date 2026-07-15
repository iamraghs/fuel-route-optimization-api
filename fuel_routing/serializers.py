"""Serializers for Fuel Route Optimization API - Production-Grade Format."""
from decimal import Decimal
from rest_framework import serializers


# ============================================================================
# CLEAN RESPONSE SERIALIZERS (NO INTERNAL DATA, PROPER PRECISION)
# ============================================================================

class RequestLocationSerializer(serializers.Serializer):
    """Request location info (for UI display)."""
    city = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=32)
    formatted_address = serializers.CharField(max_length=255)


class RequestInfoSerializer(serializers.Serializer):
    """Request information."""
    start = RequestLocationSerializer()
    finish = RequestLocationSerializer()


class FuelStopSerializer(serializers.Serializer):
    """Fuel stop - CLEAN FORMAT (no coordinates, proper precision)."""
    stop_number = serializers.IntegerField()
    station_name = serializers.CharField(max_length=255)
    brand = serializers.CharField(max_length=100, required=False)
    city = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=2)
    address = serializers.CharField(max_length=255, required=False)
    mile_marker = serializers.SerializerMethodField()
    fuel_price_per_gallon = serializers.SerializerMethodField()
    gallons_to_buy = serializers.SerializerMethodField()
    fuel_cost = serializers.SerializerMethodField()
    detour_miles = serializers.SerializerMethodField()
    def get_mile_marker(self, obj):
        """Round mile marker to 1 decimal place."""
        value = obj.get('mile_marker', 0)
        return round(float(value), 1)

    def get_fuel_price_per_gallon(self, obj):
        """Format price with DB precision."""
        value = obj.get('fuel_price_per_gallon', 0)
        return float(value)

    def get_gallons_to_buy(self, obj):
        """Round gallons to 1 decimal place."""
        value = obj.get('gallons_to_buy', 0)
        return round(float(value), 1)

    def get_fuel_cost(self, obj):
        """Round cost to 2 decimal places (cents)."""
        value = obj.get('fuel_cost', 0)
        if isinstance(value, Decimal):
            return float(value)
        return round(float(value), 2)

    def get_detour_miles(self, obj):
        """Detour miles from highway to station (round trip = 2x)."""
        value = obj.get('detour_miles', 0)
        return round(float(value), 1)


class SelectedRouteSerializer(serializers.Serializer):
    """Selected route details - CLEAN FORMAT (proper precision)."""
    route_id = serializers.CharField(max_length=20)
    distance_miles = serializers.SerializerMethodField()
    is_optimal = serializers.BooleanField(required=False)
    reason = serializers.CharField(required=False, allow_null=True)
    estimated_total_fuel_consumption_gallons = serializers.SerializerMethodField()
    estimated_total_fuel_cost = serializers.SerializerMethodField()
    fuel_stops_required = serializers.IntegerField()
    route_map_link = serializers.CharField()
    warning = serializers.CharField(required=False, allow_null=True)
    
    def get_distance_miles(self, obj):
        """Round to 1 decimal place."""
        value = obj.get('distance_miles', 0)
        return round(float(value), 1)
    
    def get_estimated_total_fuel_consumption_gallons(self, obj):
        """Round to 1 decimal place."""
        value = obj.get('estimated_total_fuel_consumption_gallons', 0)
        return round(float(value), 1)
    
    def get_estimated_total_fuel_cost(self, obj):
        """Round to 2 decimal places (cents)."""
        value = obj.get('estimated_total_fuel_cost', 0)
        if isinstance(value, Decimal):
            return float(value)
        return round(float(value), 2)


class RouteComparisonSerializer(serializers.Serializer):
    """Route comparison - CLEAN FORMAT (proper precision)."""
    route_id = serializers.CharField(max_length=20)
    distance_miles = serializers.SerializerMethodField()
    estimated_total_fuel_cost = serializers.SerializerMethodField()
    fuel_stops_required = serializers.IntegerField()
    selected = serializers.BooleanField(required=False)  # ✅ Indicates which route was selected
    
    def get_distance_miles(self, obj):
        """Round to 1 decimal place."""
        value = obj.get('distance_miles', 0)
        return round(float(value), 1)
    
    def get_estimated_total_fuel_cost(self, obj):
        """Round to 2 decimal places (cents)."""
        value = obj.get('estimated_total_fuel_cost', 0)
        if isinstance(value, Decimal):
            return float(value)
        return round(float(value), 2)


class TripSummarySerializer(serializers.Serializer):
    """Trip summary statistics (proper precision) with complete fuel accounting."""
    # ✅ Core metrics
    total_distance_miles = serializers.SerializerMethodField()
    total_fuel_consumed_gallons = serializers.SerializerMethodField()
    total_fuel_cost = serializers.SerializerMethodField()
    average_price_per_gallon = serializers.SerializerMethodField()
    total_fuel_stops = serializers.IntegerField()
    
    # ✅ Complete fuel accounting (for verification and transparency)
    starting_fuel_gallons = serializers.SerializerMethodField()
    fuel_purchased_at_stops = serializers.SerializerMethodField()
    total_fuel_available = serializers.SerializerMethodField()
    fuel_remaining_at_destination = serializers.SerializerMethodField()
    
    def get_total_distance_miles(self, obj):
        """Round to 1 decimal place."""
        value = obj.get('total_distance_miles', 0)
        return round(float(value), 1)
    
    def get_total_fuel_consumed_gallons(self, obj):
        """Round to 1 decimal place."""
        value = obj.get('total_fuel_consumed_gallons', 0)
        return round(float(value), 1)
    
    def get_total_fuel_cost(self, obj):
        """Round to 2 decimal places (cents)."""
        value = obj.get('total_fuel_cost', 0)
        if isinstance(value, Decimal):
            return float(value)
        return round(float(value), 2)
    
    def get_average_price_per_gallon(self, obj):
        """Format price to 2 decimal places (cents)."""
        value = obj.get('average_price_per_gallon', 0)
        return round(float(value), 2)
    
    def get_starting_fuel_gallons(self, obj):
        """Starting fuel (full tank assumption)."""
        value = obj.get('starting_fuel_gallons', 50)
        return round(float(value), 1)
    
    def get_fuel_purchased_at_stops(self, obj):
        """Total fuel purchased at all stops."""
        value = obj.get('fuel_purchased_at_stops', 0)
        return round(float(value), 1)
    
    def get_total_fuel_available(self, obj):
        """Total fuel available = starting + purchased."""
        value = obj.get('total_fuel_available', 0)
        return round(float(value), 1)
    
    def get_fuel_remaining_at_destination(self, obj):
        """Fuel remaining when reaching destination."""
        value = obj.get('fuel_remaining_at_destination', 0)
        return round(float(value), 1)


# ============================================================================
# REQUEST/RESPONSE SERIALIZERS
# ============================================================================

class FuelOptimizationRequestSerializer(serializers.Serializer):
    """Fuel optimization API request."""
    start = serializers.JSONField(help_text="Start location: address or {'lat': x, 'lng': y}")
    finish = serializers.JSONField(help_text="End location: address or {'lat': x, 'lng': y}")
    
    def validate_start(self, value):
        """Validate start location format."""
        if isinstance(value, str):
            if 1 <= len(value) <= 1024:
                return value
            raise serializers.ValidationError(
                "Address must be between 1 and 1024 characters"
            )
        if isinstance(value, dict):
            lat = value.get('lat')
            if lat is None:
                lat = value.get('latitude')
            lng = value.get('lng')
            if lng is None:
                lng = value.get('longitude')
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                if not (-90.0 <= lat <= 90.0):
                    raise serializers.ValidationError(
                        "Latitude must be between -90 and 90."
                    )
                if not (-180.0 <= lng <= 180.0):
                    raise serializers.ValidationError(
                        "Longitude must be between -180 and 180."
                    )
                return value
        raise serializers.ValidationError("Must be address string or {'lat': x, 'lng': y}")

    def validate_finish(self, value):
        """Validate finish location format."""
        if isinstance(value, str):
            if 1 <= len(value) <= 1024:
                return value
            raise serializers.ValidationError(
                "Address must be between 1 and 1024 characters"
            )
        if isinstance(value, dict):
            lat = value.get('lat')
            if lat is None:
                lat = value.get('latitude')
            lng = value.get('lng')
            if lng is None:
                lng = value.get('longitude')
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                if not (-90.0 <= lat <= 90.0):
                    raise serializers.ValidationError(
                        "Latitude must be between -90 and 90."
                    )
                if not (-180.0 <= lng <= 180.0):
                    raise serializers.ValidationError(
                        "Longitude must be between -180 and 180."
                    )
                return value
        raise serializers.ValidationError("Must be address string or {'lat': x, 'lng': y}")


class FuelOptimizationResponseSerializer(serializers.Serializer):
    """Fuel optimization API response - CLEAN, PRODUCTION FORMAT (proper precision)."""

    status = serializers.CharField(required=False)
    request_id = serializers.CharField(required=False)
    request = RequestInfoSerializer()
    selected_route = SelectedRouteSerializer()
    route_comparison = RouteComparisonSerializer(many=True)
    fuel_stops = FuelStopSerializer(many=True)
    trip_summary = TripSummarySerializer()


