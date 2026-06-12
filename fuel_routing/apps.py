"""Application configuration for fuel_routing app."""
from django.apps import AppConfig


class FuelRoutingConfig(AppConfig):
    """Configuration class for fuel_routing application."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'fuel_routing'
    verbose_name = 'Fuel Routing Optimization'
    
    def ready(self):
        """Initialize app."""
        import logging
        logger = logging.getLogger(__name__)
        logger.debug("Fuel Routing app is ready")



