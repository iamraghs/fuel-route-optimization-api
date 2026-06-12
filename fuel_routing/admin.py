"""Django Admin Configuration for Fuel Routing Models - Enhanced UX."""
from django.contrib import admin
from django.utils.html import format_html
from .models import (
    FuelStation, FuelPrice, PriceVersion, RouteCache,
    RouteRequest, GeocodeFailure
)


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    """Enhanced admin interface for Fuel Stations with better readability."""
    
    # Primary list view
    list_display = (
        'formatted_name', 'city_state_display', 'geocode_status',
        'price_status', 'is_active_display', 'coordinates_display'
    )
    
    # Filters for quick navigation
    list_filter = (
        'state', 'is_active', 'city', 'created_at',
    )
    
    # Search capability
    search_fields = (
        'name', 'city', 'state', 'address', 'opis_id'
    )
    
    # Readonly fields (computed/immutable)
    readonly_fields = (
        'created_at', 'updated_at', 'last_verified_at',
        'coordinates_tuple', 'formatted_full_address',
        'geocode_status_detail', 'price_status_detail',
        'brand_extracted'
    )
    
    # Organize fields into logical groups
    fieldsets = (
        ('Station Identity', {
            'fields': ('opis_id', 'name', 'brand_extracted')
        }),
        ('Location & Address', {
            'fields': (
                'city', 'state', 'country', 'address',
                'formatted_full_address'
            )
        }),
        ('Geographic Coordinates (Precision 0.1m)', {
            'fields': (
                ('latitude', 'longitude'),
                'coordinates_tuple',
                'location', 'location_geography'
            ),
            'classes': ('collapse',),
            'description': 'Stored with DecimalField(9,6) for GPS precision. '
                          '6 decimal places = ~0.1 meter accuracy.'
        }),
        ('Operational Status', {
            'fields': ('is_active', 'quality_score', 'rack_id')
        }),
        ('Geocoding & Data Quality', {
            'fields': (
                'geocode_status_detail',
                'price_status_detail',
                'last_verified_at',
                'data_source_version'
            ),
            'classes': ('collapse',)
        }),
        ('Audit Trail', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def formatted_name(self, obj):
        """Station name with brand highlight."""
        return f"{obj.name}"
    formatted_name.short_description = "Station"
    
    def city_state_display(self, obj):
        """City, State in clean format."""
        return f"{obj.city}, {obj.state}"
    city_state_display.short_description = "Location"
    
    def geocode_status(self, obj):
        """Visual geocoding status indicator."""
        if obj.latitude and obj.longitude:
            return format_html(
                '<span style="color: #0d7300; font-weight: bold;">✓ Geocoded</span>'
            )
        return format_html(
            '<span style="color: #cc0000; font-weight: bold;">✗ Pending</span>'
        )
    geocode_status.short_description = "Geocode"
    
    def geocode_status_detail(self, obj):
        """Detailed geocoding status (readonly)."""
        if obj.latitude and obj.longitude:
            return f"✓ Geocoded at {obj.last_verified_at}"
        return "✗ Geocoding failed - needs retry"
    geocode_status_detail.short_description = "Geocoding Status"
    
    def price_status(self, obj):
        """Visual price availability indicator."""
        from .models import FuelPrice, PriceVersion
        
        try:
            active_version = PriceVersion.objects.filter(is_active=True).first()
            if not active_version:
                return format_html(
                    '<span style="color: #ff9900;">⚠ No active price</span>'
                )
            
            price = FuelPrice.objects.filter(
                opis_id=obj.opis_id,
                version=active_version,
                is_available=True
            ).first()
            
            if price:
                return format_html(
                    '<span style="color: #0066cc; font-weight: bold;">'
                    '$ {:.2f}</span>',
                    float(price.price_per_gallon)
                )
            else:
                return format_html(
                    '<span style="color: #cc6600;">⚠ No price</span>'
                )
        except Exception:
            return "—"
    
    price_status.short_description = "Fuel Price"
    
    def price_status_detail(self, obj):
        """Detailed price status (readonly)."""
        from .models import FuelPrice, PriceVersion
        
        try:
            active_version = PriceVersion.objects.filter(is_active=True).first()
            if not active_version:
                return "No active price version"
            
            price = FuelPrice.objects.filter(
                opis_id=obj.opis_id,
                version=active_version
            ).first()
            
            if price:
                status = "Available" if price.is_available else "Unavailable"
                return f"${float(price.price_per_gallon):.2f}/gal ({status})"
            else:
                return "No price record found"
        except Exception as e:
            return f"Error: {str(e)}"
    
    price_status_detail.short_description = "Price Status"
    
    def is_active_display(self, obj):
        """Visual active status."""
        if obj.is_active:
            return format_html(
                '<span style="color: #0d7300; font-weight: bold;">●</span>'
            )
        return format_html(
            '<span style="color: #cc0000; font-weight: bold;">●</span>'
        )
    
    is_active_display.short_description = "Status"
    
    def coordinates_display(self, obj):
        """Coordinates in clean 6-decimal format."""
        if obj.latitude and obj.longitude:
            return f"{float(obj.latitude):.6f}, {float(obj.longitude):.6f}"
        return "Not geocoded"
    
    coordinates_display.short_description = "Coordinates"
    
    def coordinates_tuple(self, obj):
        """Display as tuple (readonly in form)."""
        if obj.latitude and obj.longitude:
            lat = float(obj.latitude)
            lng = float(obj.longitude)
            return f"({lat:.6f}, {lng:.6f})"
        return "Not geocoded"
    
    coordinates_tuple.short_description = "Coordinates (Tuple)"
    
    def formatted_full_address(self, obj):
        """Full address with city and state."""
        return f"{obj.address}, {obj.city}, {obj.state}"
    
    formatted_full_address.short_description = "Full Address"
    
    def brand_extracted(self, obj):
        """Extract brand from station name."""
        name = obj.name.upper()
        brands = ['LOVES', 'PILOT', 'FLYING J', 'TA', 'PETRO', 'SPEEDWAY', 'SHELL', 'CHEVRON']
        for brand in brands:
            if brand in name:
                return brand
        return "Unknown"
    
    brand_extracted.short_description = "Brand (extracted)"


@admin.register(FuelPrice)
class FuelPriceAdmin(admin.ModelAdmin):
    """Enhanced admin interface for Fuel Prices."""
    
    list_display = (
        'opis_id', 'fuel_type', 'price_display',
        'version', 'is_current', 'is_available_display', 'observed_at'
    )
    
    list_filter = ('fuel_type', 'is_current', 'is_available', 'observed_at', 'version')
    search_fields = ('opis_id', 'fuel_type')
    readonly_fields = ('created_at',)
    
    fieldsets = (
        ('Price Info', {
            'fields': ('version', 'opis_id', 'fuel_type', 'price_per_gallon')
        }),
        ('Timing', {
            'fields': ('effective_from', 'effective_until', 'observed_at')
        }),
        ('Status', {
            'fields': ('is_current', 'is_available')
        }),
        ('Metadata', {
            'fields': ('created_at',),
            'classes': ('collapse',)
        }),
    )
    
    def price_display(self, obj):
        """Format price with $ symbol."""
        return f"${float(obj.price_per_gallon):.3f}"
    
    price_display.short_description = "Price/Gallon"
    
    def is_available_display(self, obj):
        """Visual availability status."""
        if obj.is_available:
            return format_html('<span style="color: #0d7300;">●</span>')
        return format_html('<span style="color: #cc0000;">●</span>')
    
    is_available_display.short_description = "Available"


@admin.register(PriceVersion)
class PriceVersionAdmin(admin.ModelAdmin):
    """Enhanced admin interface for Price Versions."""
    
    list_display = (
        'version_display', 'status_display', 'total_stations',
        'total_records', 'created_at', 'expires_at'
    )
    
    list_filter = ('is_active', 'is_published', 'created_at')
    readonly_fields = ('version_hash', 'created_at', 'published_at')
    
    fieldsets = (
        ('Version Info', {
            'fields': ('version_number', 'version_hash')
        }),
        ('Status', {
            'fields': ('is_active', 'is_published')
        }),
        ('Statistics', {
            'fields': ('total_stations', 'total_records')
        }),
        ('Expiration', {
            'fields': ('expires_at',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'published_at'),
            'classes': ('collapse',)
        }),
    )
    
    def version_display(self, obj):
        """Version number display."""
        return f"v{obj.version_number}"
    
    version_display.short_description = "Version"
    
    def status_display(self, obj):
        """Status with color indicators."""
        if obj.is_active:
            return format_html(
                '<span style="color: #0d7300; font-weight: bold;">ACTIVE</span>'
            )
        elif obj.is_published:
            return format_html(
                '<span style="color: #0066cc; font-weight: bold;">PUBLISHED</span>'
            )
        else:
            return format_html(
                '<span style="color: #ff9900; font-weight: bold;">DRAFT</span>'
            )
    
    status_display.short_description = "Status"


@admin.register(RouteCache)
class RouteCacheAdmin(admin.ModelAdmin):
    """Admin interface for Route Caches."""
    
    list_display = (
        'route_display', 'distance_display', 'access_count', 'expires_at'
    )
    
    list_filter = ('is_valid', 'created_at', 'expires_at')
    search_fields = ('start_address', 'end_address', 'cache_key')
    readonly_fields = ('created_at', 'last_accessed_at', 'cache_key')
    
    fieldsets = (
        ('Route', {
            'fields': ('cache_key', 'start_address', 'end_address', 'total_distance_miles')
        }),
        ('Coordinates', {
            'fields': (
                ('start_lat', 'start_lon'),
                ('end_lat', 'end_lon')
            )
        }),
        ('Google Response', {
            'fields': ('google_polyline', 'google_duration_secs', 'google_api_cost')
        }),
        ('Status', {
            'fields': ('is_valid', 'route_version')
        }),
        ('Cache Stats', {
            'fields': ('access_count', 'computation_time_ms')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'last_accessed_at', 'expires_at'),
            'classes': ('collapse',)
        }),
    )
    
    def route_display(self, obj):
        """Route display."""
        return f"{obj.start_address[:20]}... → {obj.end_address[:20]}..."
    
    route_display.short_description = "Route"
    
    def distance_display(self, obj):
        """Distance display."""
        return f"{obj.total_distance_miles:.1f} mi"
    
    distance_display.short_description = "Distance"


@admin.register(RouteRequest)
class RouteRequestAdmin(admin.ModelAdmin):
    """Admin interface for Route Requests (audit log)."""
    
    list_display = (
        'request_id_display', 'addresses_display',
        'cache_hit_display', 'optimization_time_display', 'requested_at'
    )
    
    list_filter = ('cache_hit', 'requested_at', 'google_api_calls')
    search_fields = ('request_id', 'start_address', 'end_address')
    readonly_fields = (
        'request_id', 'requested_at', 'completed_at',
        'total_distance_miles', 'total_fuel_cost', 'fuel_stop_count'
    )
    
    fieldsets = (
        ('Request ID', {
            'fields': ('request_id',)
        }),
        ('Route Definition', {
            'fields': ('start_address', 'end_address')
        }),
        ('Result', {
            'fields': (
                'total_distance_miles', 'total_fuel_cost',
                'fuel_stop_count'
            )
        }),
        ('Performance', {
            'fields': (
                'optimization_time_ms', 'cache_hit', 'google_api_calls'
            )
        }),
        ('Client Info', {
            'fields': ('client_ip', 'user_id', 'api_version'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('requested_at', 'completed_at'),
            'classes': ('collapse',)
        }),
    )
    
    def request_id_display(self, obj):
        """Request ID display."""
        return obj.request_id[:8] + "..."
    
    request_id_display.short_description = "Request"
    
    def addresses_display(self, obj):
        """Addresses display."""
        start = obj.start_address[:20]
        end = obj.end_address[:20]
        return f"{start}... → {end}..."
    
    addresses_display.short_description = "Route"
    
    def cache_hit_display(self, obj):
        """Cache hit visual indicator."""
        if obj.cache_hit:
            return format_html(
                '<span style="color: #0d7300; font-weight: bold;">✓ HIT</span>'
            )
        return format_html(
            '<span style="color: #cc0000;">✗ MISS</span>'
        )
    
    cache_hit_display.short_description = "Cache"
    
    def optimization_time_display(self, obj):
        """Optimization time display."""
        return f"{obj.optimization_time_ms}ms"
    
    optimization_time_display.short_description = "Time"


@admin.register(GeocodeFailure)
class GeocodeFailureAdmin(admin.ModelAdmin):
    """Admin interface for Geocoding Failures."""
    
    list_display = (
        'original_address', 'city_state',
        'failure_status', 'next_retry_at'
    )
    
    list_filter = ('created_at', 'next_retry_at')
    search_fields = ('original_address', 'city', 'state')
    readonly_fields = ('created_at',)
    
    def failure_status(self, obj):
        """Visual failure status."""
        if obj.is_resolved:
            return format_html(
                '<span style="color: #0d7300;">✓ FIXED</span>'
            )
        return format_html(
            '<span style="color: #cc0000;">✗ FAILED</span>'
        )
    
    failure_status.short_description = "Status"
    
    def city_state(self, obj):
        """City, State display."""
        return f"{obj.city}, {obj.state}"
    
    city_state.short_description = "Location"



