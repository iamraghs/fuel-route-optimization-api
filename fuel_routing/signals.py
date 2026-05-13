"""Django signals for fuel_routing app."""
import logging
from django.db.models.signals import post_migrate
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_migrate)
def load_truck_stop_data(sender, **kwargs):
    """
    Initialize signal receiver (legacy - data now loaded via management command).
    
    This is called automatically after database migrations are complete.
    Use: python manage.py load_fuel_data <csv_file>
    """
    logger.debug("Post-migrate signal received (data loading via management command)")
