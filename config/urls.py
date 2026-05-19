"""URL configuration for Spotter AI Fuel Routing API."""
from django.contrib import admin
from django.urls import path
from fuel_routing.api import optimize_fuel_route
from fuel_routing.views import health_check

urlpatterns = [

    path('admin/', admin.site.urls),

    # Health check
    path('route/health/', health_check, name='health-check'),

    # Main API endpoint
    path('route/fuel-optimization/', optimize_fuel_route, name='optimize-fuel-route'),
]

