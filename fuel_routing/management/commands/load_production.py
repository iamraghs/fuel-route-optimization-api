"""
Production data loading management command.

Usage:
  python manage.py load_fuel_data <csv_file>
  python manage.py load_fuel_data fuel-prices-for-be-assessment.csv --batch-size 500
"""

from django.core.management.base import BaseCommand, CommandError
from fuel_routing.data_loader import FuelDataLoader
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Load fuel pricing data from CSV file'
    
    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to CSV file containing fuel pricing data'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Number of records to batch together (default: 500)'
        )
        parser.add_argument(
            '--skip-geocoding',
            action='store_true',
            help='Skip geocoding, use state centroids instead'
        )
    
    def handle(self, *args, **options):
        csv_file = options['csv_file']
        batch_size = options['batch_size']
        skip_geocoding = options['skip_geocoding']
        
        self.stdout.write(self.style.SUCCESS(f'Loading fuel data from {csv_file}'))
        
        try:
            loader = FuelDataLoader(csv_file)
            stats = loader.load()
            
            # Display results
            self.stdout.write('\n' + '='*60)
            self.stdout.write(self.style.SUCCESS('✅ LOAD COMPLETE'))
            self.stdout.write('='*60)
            self.stdout.write(f'Total Records Read:  {stats["total_rows"]:,}')
            self.stdout.write(f'Valid Records:       {stats["valid_rows"]:,}')
            self.stdout.write(f'Loaded to Database:  {stats["loaded"]:,}')
            self.stdout.write(f'Duplicates Removed:  {stats["duplicates_removed"]:,}')
            
            if stats['invalid_state'] > 0:
                self.stdout.write(self.style.WARNING(f'Invalid States:      {stats["invalid_state"]}'))
            
            if stats['invalid_price'] > 0:
                self.stdout.write(self.style.WARNING(f'Invalid Prices:      {stats["invalid_price"]}'))
            
            self.stdout.write('='*60)
            
            # Verify database
            from fuel_routing.models import TruckStop
            total = TruckStop.objects.count()
            states = TruckStop.objects.values('state').distinct().count()
            
            self.stdout.write(f'\n✅ Database Verification:')
            self.stdout.write(f'   Total Stops:      {total:,}')
            self.stdout.write(f'   States Covered:   {states}')
            
            if total > 0:
                prices = TruckStop.objects.aggregate(
                    min_price=__import__('django.db.models', fromlist=['Min']).Min('retail_price'),
                    max_price=__import__('django.db.models', fromlist=['Max']).Max('retail_price'),
                    avg_price=__import__('django.db.models', fromlist=['Avg']).Avg('retail_price')
                )
                self.stdout.write(f'   Price Range:      ${prices["min_price"]:.2f} - ${prices["max_price"]:.2f}')
                self.stdout.write(f'   Average Price:    ${prices["avg_price"]:.2f}/gal')
            
            self.stdout.write(self.style.SUCCESS('\n✅ Ready to serve routes!\n'))
        
        except FileNotFoundError as e:
            raise CommandError(f'CSV file not found: {csv_file}')
        except Exception as e:
            logger.error(f'Loading failed: {e}', exc_info=True)
            raise CommandError(f'Loading failed: {e}')
