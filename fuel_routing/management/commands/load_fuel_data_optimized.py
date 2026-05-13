"""
Django management command for optimized fuel data loading.

Usage:
    python manage.py load_fuel_data_optimized fuel-prices-for-be-assessment.csv
    python manage.py load_fuel_data_optimized fuel-prices-for-be-assessment.csv --batch-size 200
    python manage.py load_fuel_data_optimized fuel-prices-for-be-assessment.csv --workers 8
    python manage.py load_fuel_data_optimized fuel-prices-for-be-assessment.csv --skip-preprocess
"""

import logging
from django.core.management.base import BaseCommand
from fuel_routing.optimized_loader import load_optimized_fuel_data

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Load fuel pricing data using optimized parallel processing'
    
    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file',
            type=str,
            help='Path to CSV file to load'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Records per batch (default: 100)'
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=4,
            help='Number of parallel workers (default: 4)'
        )
        parser.add_argument(
            '--skip-preprocess',
            action='store_true',
            help='Skip preprocessing step (not recommended)'
        )
    
    def handle(self, *args, **options):
        csv_file = options['csv_file']
        batch_size = options['batch_size']
        num_workers = options['workers']
        preprocess = not options['skip_preprocess']
        
        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*80}\n"
            f"⚡ OPTIMIZED FUEL DATA LOADER\n"
            f"{'='*80}\n"
            f"File:          {csv_file}\n"
            f"Batch Size:    {batch_size} records\n"
            f"Workers:       {num_workers} parallel\n"
            f"Preprocessing: {'Yes' if preprocess else 'No'}\n"
        ))
        
        try:
            stats = load_optimized_fuel_data(
                csv_file, 
                batch_size=batch_size, 
                num_workers=num_workers,
                preprocess=preprocess
            )
            
            self.stdout.write(self.style.SUCCESS(
                f"\n✅ Load Complete!\n"
                f"   Loaded: {stats['loaded']:,} records\n"
                f"   Time: {stats['time_preprocessing_sec'] + stats['time_loading_sec']:.1f}s\n"
            ))
        
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"❌ File not found: {csv_file}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Error: {str(e)}"))
