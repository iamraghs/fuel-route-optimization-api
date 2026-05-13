"""Application configuration for fuel_routing app."""
from django.apps import AppConfig


class FuelRoutingConfig(AppConfig):
    """Configuration class for fuel_routing application."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'fuel_routing'
    verbose_name = 'Fuel Routing Optimization'
    
    def ready(self):
        """Initialize app - load data on startup."""
        import logging
        from django.db.models.signals import post_migrate
        from .signals import load_truck_stop_data
        
        logger = logging.getLogger(__name__)
        logger.debug("Fuel Routing app is ready")
        
        # Auto-load truck stop data after migrations (disabled for now due to decimal conversion issues)
        # post_migrate.connect(load_truck_stop_data, sender=self)
