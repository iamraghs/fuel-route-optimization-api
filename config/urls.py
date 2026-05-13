"""URL configuration for Spotter AI Fuel Routing API."""
from django.contrib import admin
from django.urls import path
from fuel_routing.api import optimize_fuel_route

urlpatterns = [

    path('admin/', admin.site.urls),
    
    # Main API endpoint
    path('route/fuel-optimization', optimize_fuel_route, name='optimize-fuel-route'),
]

