"""Shared configuration constants derived from Django settings."""
from django.conf import settings

VEHICLE_MPG = settings.VEHICLE_FUEL_EFFICIENCY        # 10 MPG
VEHICLE_TANK = settings.VEHICLE_FUEL_TANK_CAPACITY     # 50 gallons
VEHICLE_MAX_RANGE = settings.VEHICLE_MAX_RANGE          # 500 miles
VEHICLE_RESERVE_MILES = settings.VEHICLE_RESERVE_RANGE_MILES  # 50 miles

CORRIDOR_BUFFER_MILES = settings.FUEL_STOP_CORRIDOR_BUFFER_MILES  # 50 miles
MAX_DETOUR_MILES = settings.FUEL_STOP_MAX_DETOUR_MILES            # 5 miles

MIN_DESTINATION_RESERVE_GALLONS = 5  # Minimum fuel remaining at destination (safety reserve)

# Cache TTLs (centralized from settings to avoid per-module settings import)
CACHE_TTL_GEOCODE = settings.CACHE_TTL['GEOCODING']   # 7 days
CACHE_TTL_ROUTE = settings.CACHE_TTL['ROUTE_GEOMETRY']  # 24 hours

GOOGLE_API_KEY = settings.GOOGLE_MAPS_API_KEY
GOOGLE_ROUTES_ENDPOINT = settings.GOOGLE_ROUTES_API_ENDPOINT
GOOGLE_GEOCODING_ENDPOINT = settings.GOOGLE_GEOCODING_API_ENDPOINT

