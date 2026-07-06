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

        # Corridor cache invalidation on station changes
        from django.db.models.signals import post_save, post_delete
        from .models import FuelStation
        from .cache_utils import CorridorStationCache

        def bump_corridor_cache(sender, instance, **kwargs):
            CorridorStationCache.bump_station_version()

        post_save.connect(bump_corridor_cache, sender=FuelStation, weak=False)
        post_delete.connect(bump_corridor_cache, sender=FuelStation, weak=False)



